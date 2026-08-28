"""Rescan requests to Plex / Jellyfin / Navidrome, and the run M3U.

The headless NAS case the project already serves has one missing step at
the end: the library server doesn't know anything changed until its own
scheduled scan.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.parse

import pytest

from SpotiFLAC.core import library_notify as ln
from SpotiFLAC.core.loop_runner import run_sync
from SpotiFLAC.core.models import DownloadResult
from SpotiFLAC.core.report import RunReport


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    monkeypatch.delenv(ln.LIBRARY_TOKEN_ENV, raising=False)
    monkeypatch.delenv(ln.LIBRARY_USER_ENV, raising=False)


# ── target construction ────────────────────────────────────────────────────


def test_an_unknown_server_type_is_rejected() -> None:
    with pytest.raises(ln.LibraryNotifyError, match="Unknown library type"):
        ln.build_target("winamp", "http://x", token="t")


def test_a_missing_url_is_rejected() -> None:
    with pytest.raises(ln.LibraryNotifyError, match="URL is required"):
        ln.build_target("plex", "", token="t")


def test_a_missing_token_names_the_environment_variable() -> None:
    with pytest.raises(ln.LibraryNotifyError, match=ln.LIBRARY_TOKEN_ENV):
        ln.build_target("plex", "http://x")


def test_the_token_can_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ln.LIBRARY_TOKEN_ENV, "from-env")
    assert ln.build_target("plex", "http://x").token == "from-env"


def test_an_explicit_token_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ln.LIBRARY_TOKEN_ENV, "from-env")
    assert ln.build_target("plex", "http://x", token="explicit").token == "explicit"


def test_navidrome_also_needs_a_username() -> None:
    with pytest.raises(ln.LibraryNotifyError, match="username"):
        ln.build_target("navidrome", "http://x", token="pw")


def test_a_trailing_slash_in_the_url_is_harmless() -> None:
    target = ln.build_target("plex", "http://x:32400/", token="t")
    assert target.base == "http://x:32400"


# ── per-server request shapes ──────────────────────────────────────────────


def test_plex_sends_its_token_as_a_query_parameter() -> None:
    method, url, params, headers = ln.build_request(
        ln.build_target("plex", "http://x:32400", token="plex-token")
    )
    assert method == "GET"
    assert url.endswith("/library/sections/all/refresh")
    assert params["X-Plex-Token"] == "plex-token"
    assert headers == {}


@pytest.mark.parametrize("kind", ["jellyfin", "emby"])
def test_jellyfin_family_posts_with_a_header(kind) -> None:
    method, url, params, headers = ln.build_request(
        ln.build_target(kind, "http://x:8096", token="api-key")
    )
    assert method == "POST"
    assert url.endswith("/Library/Refresh")
    assert headers["X-Emby-Token"] == "api-key"


def test_subsonic_never_sends_the_password_itself() -> None:
    """The Subsonic API also accepts a plaintext `p=` parameter. Using the
    salted-hash form instead keeps the password out of the URL, and out of
    the server's access log.
    """
    _, url, params, _ = ln.build_request(
        ln.build_target("navidrome", "http://x:4533", token="secret", username="alice")
    )
    assert url.endswith("/rest/startScan")
    assert params["u"] == "alice"
    assert "p" not in params
    assert "secret" not in str(params)
    assert len(params["t"]) == 32 and params["s"]


def test_the_subsonic_salt_changes_every_time() -> None:
    target = ln.build_target("navidrome", "http://x", token="secret", username="alice")
    first = ln.build_request(target)[2]["s"]
    second = ln.build_request(target)[2]["s"]
    assert first != second


# ── actually calling a server ──────────────────────────────────────────────


class _Recorder(http.server.BaseHTTPRequestHandler):
    def _handle(self):
        self.server.seen.append(  # type: ignore[attr-defined]
            (self.command, self.path, dict(self.headers))
        )
        code = self.server.status  # type: ignore[attr-defined]
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Recorder)
    srv.daemon_threads = True
    srv.seen = []
    srv.status = 200
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


def test_a_plex_rescan_reaches_the_server(server) -> None:
    target = ln.build_target("plex", server.url, token="plex-token")
    assert run_sync(ln.request_rescan(target)) is True

    method, path, _ = server.seen[0]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    assert method == "GET"
    assert query["X-Plex-Token"] == ["plex-token"]


def test_a_jellyfin_rescan_sends_the_token_header(server) -> None:
    target = ln.build_target("jellyfin", server.url, token="api-key")
    assert run_sync(ln.request_rescan(target)) is True

    method, path, headers = server.seen[0]
    assert method == "POST"
    assert path == "/Library/Refresh"
    assert headers["X-Emby-Token"] == "api-key"


def test_a_server_error_is_reported_but_never_raises(server) -> None:
    """The files are on disk either way; a library server being down must
    not turn a completed download into a failed run.
    """
    server.status = 500
    target = ln.build_target("plex", server.url, token="t")
    assert run_sync(ln.request_rescan(target)) is False


def test_an_unreachable_server_never_raises() -> None:
    target = ln.build_target("plex", "http://127.0.0.1:9", token="t")
    assert run_sync(ln.request_rescan(target, timeout_s=1)) is False


# ── the run M3U ────────────────────────────────────────────────────────────


class _Meta:
    def __init__(self, title, artists, duration_ms=185000):
        self.id, self.album, self.isrc = "1", "Album", "X"
        self.title, self.artists, self.duration_ms = title, artists, duration_ms


def _report_with(tmp_path, *names):
    report = RunReport()
    for name in names:
        path = tmp_path / "music" / f"{name}.flac"
        report(
            DownloadResult(
                success=True, provider="tidal", file_path=str(path), format="flac"
            ),
            _Meta(name, "Band"),
        )
    return report


def test_the_m3u_uses_paths_relative_to_itself(tmp_path) -> None:
    """Relative paths keep the folder portable: moving or syncing it
    elsewhere leaves the playlist working.
    """
    report = _report_with(tmp_path, "One", "Two")
    content = report.to_m3u(tmp_path / "run.m3u8")

    assert content.startswith("#EXTM3U")
    assert "music/One.flac" in content
    assert str(tmp_path) not in content, "absolute path leaked into the playlist"


def test_the_m3u_carries_real_durations(tmp_path) -> None:
    content = _report_with(tmp_path, "One").to_m3u(tmp_path / "run.m3u8")
    assert "#EXTINF:185,Band - One" in content


def test_failed_tracks_are_not_listed(tmp_path) -> None:
    report = _report_with(tmp_path, "Good")
    report(DownloadResult(success=False, provider="none", error="x"), _Meta("Bad", "B"))

    content = report.to_m3u(tmp_path / "run.m3u8")
    assert "Good" in content
    assert "Bad" not in content
    assert report.downloaded_paths() == [str(tmp_path / "music" / "Good.flac")]

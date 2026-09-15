"""core/signed_session_status.py against a fake ~/.spotiflac, never the real one.

Every test builds its own sessions and extensions directories under tmp_path
and passes them explicitly, with a fixed `now`, so a state never depends on
the clock or on what happens to be installed on the machine running the suite.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from SpotiFLAC.core import signed_session_status as sss
from SpotiFLAC.core.signed_session_mobile import SignedSessionClient

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _block(app_version: str) -> dict:
    return {
        "namespace": "zarz-v2",
        "baseUrl": "https://api.example.test/v2/",
        "appVersion": app_version,
        "platform": "extension",
    }


def _install(ext_dir: Path, name: str, manifest: dict, source: str = "") -> None:
    target = ext_dir / name
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps({"name": name, **manifest}))
    if source:
        (target / f"{name}.py").write_text(source)


@pytest.fixture
def dirs(tmp_path):
    sessions = tmp_path / "signed_sessions"
    extensions = tmp_path / "extensions"
    sessions.mkdir()
    extensions.mkdir()
    return sessions, extensions


def _write(sessions: Path, name: str, record: dict) -> None:
    (sessions / name).write_text(json.dumps(record))


def test_file_stem_matches_what_the_client_writes(tmp_path):
    """The whole join rests on recomputing the client's own file name."""
    block = _block("Tidal-Web@1.2.5")
    client = SignedSessionClient(
        base_url=block["baseUrl"],
        namespace=block["namespace"],
        app_version=block["appVersion"],
        platform=block["platform"],
        data_dir=str(tmp_path),
    )
    assert client._session_path().stem == sss.gateway_file_stem(block)


def test_states_and_extension_join(dirs):
    sessions, extensions = dirs
    _install(
        extensions, "qobuz-web", {"version": "1.2.15", "signedSession": _block("q@1")}
    )
    _install(
        extensions, "tidal-web", {"version": "1.2.6", "signedSession": _block("t@1")}
    )
    _install(extensions, "deezer", {"version": "1.3.5", "signedSession": _block("d@1")})
    _install(extensions, "amazon", {"version": "2.3.8", "signedSession": _block("a@1")})
    _install(
        extensions,
        "tidal-py",
        {"version": "1.0"},
        source="from SpotiFLAC.core.signed_session_desktop import x",
    )

    live = {"install_id": "i", "session_id": "s", "session_secret": "secret"}
    _write(
        sessions,
        sss.gateway_file_stem(_block("q@1")) + ".json",
        {
            **live,
            "expires_at": _iso(timedelta(hours=2)),
            "refresh_after": _iso(timedelta(hours=1)),
            "capabilities": ["resolve"],
        },
    )
    _write(
        sessions,
        sss.gateway_file_stem(_block("t@1")) + ".json",
        {
            **live,
            "expires_at": _iso(timedelta(minutes=30)),
            "refresh_after": _iso(-timedelta(minutes=5)),
        },
    )
    _write(
        sessions,
        sss.gateway_file_stem(_block("a@1")) + ".json",
        {**live, "expires_at": _iso(-timedelta(hours=1))},
    )
    _write(
        sessions,
        sss.gateway_file_stem(_block("d@1")) + ".json",
        {"install_id": "i", "session_id": None, "session_secret": None},
    )
    _write(
        sessions,
        sss.gateway_file_stem(_block("q@0")) + ".json",
        {**live, "expires_at": _iso(timedelta(hours=2))},
    )
    _write(
        sessions,
        sss.COMMUNITY_FILE,
        {**live, "expires_at": _iso(timedelta(minutes=10))},
    )

    rows = {r["label"]: r for r in sss.list_signed_sessions(sessions, extensions, NOW)}

    assert rows["qobuz-web"]["state"] == sss.ACTIVE
    assert rows["qobuz-web"]["expires_in_s"] == 7200
    assert rows["qobuz-web"]["refresh_in_s"] == 3600
    assert rows["qobuz-web"]["version"] == "1.2.15"
    assert rows["tidal-web"]["state"] == sss.REFRESH_DUE
    assert rows["amazon"]["state"] == sss.EXPIRED
    assert rows["deezer"]["state"] == sss.UNVERIFIED
    assert rows["deezer"]["expires_in_s"] is None

    orphan = next(r for r in rows.values() if r["state"] == sss.ORPHANED)
    assert orphan["extensions"] == []

    community = rows["Community (signed-desktop)"]
    assert community["state"] == sss.ACTIVE
    assert community["extensions"] == ["tidal-py"]


def test_no_row_carries_a_secret(dirs):
    sessions, extensions = dirs
    _write(
        sessions,
        "zarz-v2-0000000000000000.json",
        {
            "install_id": "INSTALL",
            "session_id": "SID",
            "session_secret": "SECRET",
            "expires_at": _iso(timedelta(hours=1)),
        },
    )
    _write(
        sessions,
        sss.MONOCHROME_FILE,
        {"jwt": "JWT", "expires_at": _iso(timedelta(hours=1))},
    )

    dumped = json.dumps(sss.list_signed_sessions(sessions, extensions, NOW))
    for secret in ("INSTALL", "SID", "SECRET", "JWT"):
        assert secret not in dumped


def test_backoff_files_are_not_sessions_but_are_reported(dirs):
    sessions, extensions = dirs
    block = _block("q@1")
    _install(extensions, "qobuz-web", {"signedSession": block})
    stem = sss.gateway_file_stem(block)
    _write(sessions, stem + ".json", {"install_id": "i"})
    client = SignedSessionClient(
        base_url=block["baseUrl"],
        namespace=block["namespace"],
        app_version=block["appVersion"],
        platform=block["platform"],
        data_dir=str(sessions),
    )
    from SpotiFLAC.core.signed_session_mobile import _auth_backoff_path

    _auth_backoff_path(client).write_text(
        json.dumps({"failures": 1, "not_before": NOW.timestamp() + 300})
    )

    rows = sss.list_signed_sessions(sessions, extensions, NOW)
    assert [r["key"] for r in rows] == [stem]
    assert rows[0]["auth_paused_s"] == pytest.approx(300)


def test_clear_keeps_install_id(dirs):
    sessions, _ = dirs
    _write(
        sessions,
        "zarz-v2-aaaaaaaaaaaaaaaa.json",
        {
            "install_id": "keep",
            "session_id": "s",
            "session_secret": "x",
            "expires_at": _iso(timedelta(hours=1)),
            "capabilities": ["resolve"],
        },
    )
    _write(
        sessions,
        sss.COMMUNITY_FILE,
        {
            "install_id": "keep2",
            "session_id": "s",
            "session_secret": "x",
            "expires_at": _iso(timedelta(hours=1)),
        },
    )

    assert sss.clear_signed_session("zarz-v2-aaaaaaaaaaaaaaaa", sessions)
    assert sss.clear_signed_session("community_sessions", sessions)

    gateway = json.loads((sessions / "zarz-v2-aaaaaaaaaaaaaaaa.json").read_text())
    assert gateway["install_id"] == "keep"
    assert gateway["session_secret"] is None
    assert gateway["capabilities"] == []
    community = json.loads((sessions / sss.COMMUNITY_FILE).read_text())
    assert community == {
        "install_id": "keep2",
        "session_id": "",
        "session_secret": "",
        "expires_at": "",
    }
    # Windows has no POSIX mode bits: chmod is a no-op there and files are 0o666.
    if os.name != "nt":
        assert (sessions / sss.COMMUNITY_FILE).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("key", ["", "../x", "missing", ".hidden", "a/b"])
def test_clear_refuses_unknown_or_unsafe_keys(dirs, key):
    sessions, _ = dirs
    assert sss.clear_signed_session(key, sessions) is False


def test_prune_removes_only_orphans(dirs):
    sessions, extensions = dirs
    _install(extensions, "qobuz-web", {"signedSession": _block("q@2")})
    current = sss.gateway_file_stem(_block("q@2"))
    old = sss.gateway_file_stem(_block("q@1"))
    _write(sessions, current + ".json", {"install_id": "i"})
    _write(sessions, old + ".json", {"install_id": "i"})
    _write(sessions, sss.COMMUNITY_FILE, {"install_id": "i"})

    assert sss.prune_orphaned_sessions(sessions, extensions) == [old]
    assert sorted(p.name for p in sessions.iterdir()) == sorted(
        [current + ".json", sss.COMMUNITY_FILE]
    )


def test_unreadable_extensions_dir_orphans_nothing(dirs, monkeypatch):
    sessions, extensions = dirs
    old = sss.gateway_file_stem(_block("q@1"))
    _write(sessions, old + ".json", {"install_id": "i"})

    real_iterdir = Path.iterdir

    def _iterdir(self):
        if self == extensions:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)

    rows = sss.list_signed_sessions(sessions, extensions, NOW)
    assert [r["state"] for r in rows] == [sss.UNVERIFIED]
    assert sss.prune_orphaned_sessions(sessions, extensions) == []
    assert (sessions / (old + ".json")).exists()


def test_missing_directory_is_empty(tmp_path):
    assert sss.list_signed_sessions(tmp_path / "nope", tmp_path, NOW) == []


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, "—"),
        (40, "40s"),
        (720, "12m"),
        (7500, "2h 05m"),
        (-7500, "2h 05m"),
        (90000, "1d 01h"),
    ],
)
def test_format_duration(seconds, text):
    assert sss.format_duration(seconds) == text


def test_api_refuses_edits_to_non_admins(monkeypatch):
    from SpotiFLAC.api_mixins.signed_sessions import SignedSessionsMixin
    import SpotiFLAC.core.web_users as web_users

    api = SignedSessionsMixin()
    api.owner = "guest"
    monkeypatch.setattr(web_users, "is_admin", lambda name: False)
    assert api.clear_signed_session("community_sessions")["ok"] is False
    assert api.prune_signed_sessions()["ok"] is False

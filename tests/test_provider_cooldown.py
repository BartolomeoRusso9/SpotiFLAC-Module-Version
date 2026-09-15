"""A provider that answers "503 … try again in about N minutes" sits out N minutes.

tidal-py tries the Community (signed-desktop) gateway first on every track.
During one of that gateway's enforced pauses the extension logged the 503
with the length of the pause, fell back to public mirrors that timed out,
failed — and did the same on the next track, about 25 seconds each time,
before tidal-web downloaded the track in five. The pause is now read out of
that log line, and tidal-py is skipped until it is over. It is not removed:
afterwards it is first in line again.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from SpotiFLAC import downloader as dl
from SpotiFLAC.core import provider_cooldown as pc
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.extensions.runtime import JSRuntime

OVERLOADED = (
    'HTTP 503 - {"detail":"The server is overloaded and taking a short break. '
    'Please try again in about 51 minute(s)."}'
)
TIDAL_PY = "SpotiFLAC.extensions_plugins.tidal_py"
#: A JS extension has no module of its own: the bridge relays its log lines
#: under a child of the runtime logger named for the extension.
TIDAL_WEB = "SpotiFLAC.extensions.runtime.tidal-web"


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "cache"))
    yield
    logging.getLogger().removeHandler(pc._watcher)


# ---------------------------------------------------------------------------
# reading the pause
# ---------------------------------------------------------------------------


def test_the_community_503_names_its_pause_in_minutes() -> None:
    assert pc.pause_seconds(OVERLOADED) == 51 * 60


def test_a_pause_in_hours_is_read_too() -> None:
    assert pc.pause_seconds("HTTP 503: try again in 2 hours") == 2 * 3600


def test_a_503_without_a_wait_is_no_pause() -> None:
    assert pc.pause_seconds("HTTP 503 Service Unavailable") == 0


def test_other_errors_are_no_pause() -> None:
    assert pc.pause_seconds("HTTP 429: try again in about 5 minute(s)") == 0
    assert pc.pause_seconds("All 1 Tidal APIs timed out") == 0


def test_an_absurd_pause_is_capped_at_a_day() -> None:
    assert pc.pause_seconds("503 - try again in about 99999 minutes") == 24 * 3600


def test_the_key_is_the_python_extension() -> None:
    assert pc.extension_key(TIDAL_PY) == "tidal-py"
    assert pc.extension_key("SpotiFLAC.extensions.provider") == ""


def test_the_key_is_the_js_extension_too() -> None:
    assert pc.extension_key(TIDAL_WEB) == "tidal-web"
    # The runtime's own lines name no extension and pause nothing.
    assert pc.extension_key("SpotiFLAC.extensions.runtime") == ""


def test_a_js_runtime_logs_under_the_extension_s_name() -> None:
    """The other half of the key: what JSRuntime actually names the logger."""
    rt = JSRuntime(ext_path="index.js", ext_name="tidal-web")
    assert pc.extension_key(rt._ext_logger.name) == "tidal-web"
    # Unnamed, it stays on the runtime's own logger and pauses nothing.
    assert pc.extension_key(JSRuntime(ext_path="index.js")._ext_logger.name) == ""


# ---------------------------------------------------------------------------
# the log watcher
# ---------------------------------------------------------------------------


def test_the_extension_s_log_line_starts_the_pause() -> None:
    pc.watch_extension_logs()
    logging.getLogger(TIDAL_PY).warning(
        "[tidal] Community failed, falling back to public mirrors: %s", OVERLOADED
    )

    assert 50 * 60 < pc.remaining("tidal-py") <= 51 * 60
    assert (Path(pc._cache_file())).exists()


def test_a_js_extension_s_log_line_starts_the_pause() -> None:
    pc.watch_extension_logs()
    logging.getLogger(TIDAL_WEB).warning("[EXT] [tidal-web] %s", OVERLOADED)

    assert 50 * 60 < pc.remaining("tidal-web") <= 51 * 60


def test_the_same_message_from_the_host_pauses_nothing() -> None:
    pc.watch_extension_logs()
    logging.getLogger("SpotiFLAC.downloader").warning("%s", OVERLOADED)

    assert pc.remaining("tidal-py") == 0


def test_the_watcher_is_attached_once() -> None:
    pc.watch_extension_logs()
    pc.watch_extension_logs()
    assert logging.getLogger().handlers.count(pc._watcher) == 1


def test_a_shorter_pause_does_not_cut_a_longer_one(caplog) -> None:
    pc.pause("tidal-py", 3600, now=1000.0)
    pc.pause("tidal-py", 60, now=1000.0)
    assert pc.remaining("tidal-py", now=1000.0) == 3600


# ---------------------------------------------------------------------------
# skipping
# ---------------------------------------------------------------------------


class _TidalPy:
    __module__ = TIDAL_PY
    name = "tidal"


class _TidalWeb:
    name = "ext:tidal-web"


def test_a_js_provider_is_keyed_by_its_registry_name() -> None:
    """The key a JS extension's log line carries has to name its provider.

    JSExtensionProvider is the class for every JS extension, so its module
    says nothing about which one this is; "ext:tidal-web" does.
    """
    assert pc.provider_key(_TidalWeb()) == pc.extension_key(TIDAL_WEB) == "tidal-web"
    assert pc.provider_key(_TidalPy()) == "tidal-py"


def test_a_paused_js_provider_is_skipped_too() -> None:
    tidal_py, tidal_web = _TidalPy(), _TidalWeb()
    pc.pause("tidal-web", 600)

    assert pc.usable_providers([tidal_py, tidal_web]) == [tidal_py]


def test_a_native_provider_is_never_keyed() -> None:
    """Only extensions have pauses; a built-in provider is not one."""

    class _Qobuz:
        name = "qobuz"

    assert pc.provider_key(_Qobuz()) == ""


def test_a_paused_provider_is_skipped_and_the_order_kept() -> None:
    tidal_py, tidal_web = _TidalPy(), _TidalWeb()
    pc.pause("tidal-py", 600)

    assert pc.usable_providers([tidal_py, tidal_web]) == [tidal_web]


def test_it_is_first_again_once_the_pause_is_over() -> None:
    tidal_py, tidal_web = _TidalPy(), _TidalWeb()
    pc.pause("tidal-py", 600, now=0.0)  # long over

    assert pc.usable_providers([tidal_py, tidal_web]) == [tidal_py, tidal_web]


def test_with_every_provider_paused_all_of_them_are_tried() -> None:
    tidal_py = _TidalPy()
    pc.pause("tidal-py", 600)

    assert pc.usable_providers([tidal_py]) == [tidal_py]


# ---------------------------------------------------------------------------
# wired into the download
# ---------------------------------------------------------------------------


def _track(track_id: str) -> TrackMetadata:
    return TrackMetadata(
        id=track_id,
        title=f"Song {track_id}",
        artists="Artist",
        artist_names=["Artist"],
        album="Album",
        album_artist="Artist",
        isrc="",
        duration_ms=200000,
    )


class _OverloadedTidalPy:
    """What tidal-py does during a Community pause: log the 503, then fail."""

    __module__ = TIDAL_PY
    name = "tidal"

    def __init__(self) -> None:
        self.calls = 0

    def set_progress_callback(self, cb) -> None:
        pass

    async def download_track_async(self, metadata, output_dir, **kwargs):
        self.calls += 1
        logging.getLogger(TIDAL_PY).warning(
            "[tidal] Community failed, falling back to public mirrors: %s",
            OVERLOADED,
        )
        return DownloadResult.fail(self.name, "All 1 Tidal APIs timed out")


class _WorkingTidalWeb:
    name = "ext:tidal-web"

    def __init__(self, tmp_path: Path) -> None:
        self._dir = tmp_path
        self.calls = 0

    def set_progress_callback(self, cb) -> None:
        pass

    async def download_track_async(self, metadata, output_dir, **kwargs):
        self.calls += 1
        path = self._dir / f"{metadata.id}.flac"
        path.write_bytes(b"audio")
        return DownloadResult.ok(self.name, str(path))


def test_after_the_503_the_next_track_goes_straight_to_tidal_web(
    tmp_path, monkeypatch
) -> None:
    async def no_lookup(isrc):
        return None

    monkeypatch.setattr("SpotiFLAC.core.recording_guard._lookup_isrc_async", no_lookup)
    monkeypatch.setattr(dl, "_schedule_hires_check", lambda *a, **k: None)
    tidal_py, tidal_web = _OverloadedTidalPy(), _WorkingTidalWeb(tmp_path)
    opts = dl.DownloadOptions(output_dir=str(tmp_path), embed_lyrics=False)

    first = asyncio.run(
        dl.download_one_async(_track("a"), str(tmp_path), [tidal_py, tidal_web], opts)
    )
    second = asyncio.run(
        dl.download_one_async(_track("b"), str(tmp_path), [tidal_py, tidal_web], opts)
    )

    assert first.success and second.success
    assert tidal_py.calls == 1  # asked once, then left alone
    assert tidal_web.calls == 2
    assert pc.remaining("tidal-py") > 50 * 60

"""`cover_url` from a track's metadata to the queue's stats dict.

The TUI drew this field for a while and no longer does, but the route is
not the TUI's: `DownloadManager` carries `cover_url` on every queue entry
and `DownloadBroadcaster` hands it to whoever is listening — the desktop
GUI, the web frontend, and anything reading the JSON report. Dropped
anywhere along the way it leaves an artwork-shaped hole with nothing to
explain it, and the drop would be silent, because every consumer treats a
missing cover as "this track has none".
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from SpotiFLAC.core.progress import DownloadManager


def _in_a_loop(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "cache"))


@_in_a_loop
async def test_the_cover_url_reaches_the_stats_dict():
    manager = DownloadManager()
    await manager.reset()
    await manager.add_to_queue(
        "t1",
        "So What",
        "Miles Davis",
        "Kind of Blue",
        "spotify:1",
        "https://example.invalid/kind-of-blue.jpg",
    )

    stats = await manager.get_stats()

    assert stats["downloads"][0]["cover_url"] == (
        "https://example.invalid/kind-of-blue.jpg"
    )
    await manager.reset()


@_in_a_loop
async def test_a_caller_that_names_no_cover_still_works():
    """The argument is optional: the GUI and the tests predate it."""
    manager = DownloadManager()
    await manager.reset()
    await manager.add_to_queue("t1", "So What", "Miles Davis", "Kind of Blue", "s1")

    stats = await manager.get_stats()

    assert stats["downloads"][0]["cover_url"] == ""
    await manager.reset()


def test_the_downloader_passes_the_track_cover(monkeypatch):
    """The one call site — a cover dropped here is a cover never seen."""
    import SpotiFLAC.downloader as downloader_module

    seen: list[tuple] = []

    class _Manager:
        async def reset(self):
            return None

        async def add_to_queue(self, *args):
            seen.append(args)

    monkeypatch.setattr(downloader_module, "DownloadManager", lambda: _Manager())

    class _Track:
        id = "t1"
        title = "So What"
        artists = "Miles Davis"
        album = "Kind of Blue"
        external_url = ""
        cover_url = "https://example.invalid/cover.jpg"

        def model_copy(self, update):
            return self

    downloader = downloader_module.SpotiflacDownloader.__new__(
        downloader_module.SpotiflacDownloader
    )
    asyncio.run(downloader._register_queue_async([_Track()]))

    assert seen and seen[0][-1] == "https://example.invalid/cover.jpg"

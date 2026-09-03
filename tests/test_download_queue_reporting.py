"""The queue the GUI's progress is read from.

Every GUI download reported "0% · 0.00 MB/s" from start to finish, and the
queue dock never moved. The numbers were real; they were being read off an
empty queue, because DownloadWorker.run_async() called
DownloadManager.reset() *after* SpotiflacDownloader._register_queue_async()
had filled it. start_download(), complete_download() and fail_download()
then looked up ids no longer in the queue and silently did nothing.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.core.progress import DownloadManager
from SpotiFLAC.downloader import DownloadOptions, DownloadWorker, SpotiflacDownloader


def _track(track_id: str, title: str) -> TrackMetadata:
    return TrackMetadata(
        id=track_id, title=title, artists="A", album="Alb", album_artist="A"
    )


@pytest.fixture()
def downloader(tmp_path):
    # The manager is a process-wide singleton, so one test's queue would
    # otherwise be the next one's starting state.
    asyncio.run(DownloadManager().reset())
    return SpotiflacDownloader(DownloadOptions(output_dir=str(tmp_path)))


def test_registering_a_batch_leaves_it_in_the_queue(downloader):
    async def scenario():
        await downloader._register_queue_async(
            [_track("t1", "Song"), _track("t2", "Two")]
        )
        return await DownloadManager().get_stats()

    stats = asyncio.run(scenario())
    assert stats["queued"] == 2
    assert [row["track_name"] for row in stats["queue"]] == ["Song", "Two"]


def test_progress_reaches_the_stats_the_ui_reads(downloader):
    async def scenario():
        await downloader._register_queue_async(
            [_track("t1", "Song"), _track("t2", "Two")]
        )
        manager = DownloadManager()

        await manager.start_download("t1")
        assert (await manager.get_stats())["is_downloading"] is True

        await manager.complete_download("t1", "/tmp/song.flac", 12.5)
        await manager.fail_download("t2", "nope")
        return await manager.get_stats()

    stats = asyncio.run(scenario())
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["total_downloaded"] == 12.5


def test_a_new_batch_does_not_inherit_the_last_one(downloader):
    async def scenario():
        await downloader._register_queue_async([_track("old", "Old")])
        await DownloadManager().complete_download("old", "/tmp/old.flac", 5.0)

        await downloader._register_queue_async([_track("new", "New")])
        return await DownloadManager().get_stats()

    stats = asyncio.run(scenario())
    assert [row["track_name"] for row in stats["queue"]] == ["New"]
    assert stats["total_downloaded"] == 0.0


def test_the_worker_does_not_clear_the_queue_it_was_handed():
    """The ordering itself, since reproducing it needs a real download.

    Resetting inside the worker is what emptied the queue between
    registration and the first progress update.
    """
    source = inspect.getsource(DownloadWorker.run_async)
    assert "manager.reset()" not in source
    assert "reset()" in inspect.getsource(SpotiflacDownloader._register_queue_async)

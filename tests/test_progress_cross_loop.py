"""The download queue has to survive more than one event loop.

Reported from a Docker run as a hard crash at the start of a batch:

    RuntimeError: <asyncio.locks.Lock object ...> is bound to a different
    event loop

DownloadManager and DownloadBroadcaster are process-wide singletons, and
this process genuinely runs two loops — core/loop_runner.py owns a
long-lived one for the GUI and web sync bridges, while client.SpotiFLAC()
keeps its own asyncio.run() for a one-shot batch. An asyncio.Lock binds to
whichever loop touches it first and refuses every other one.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.progress import DownloadBroadcaster, DownloadManager


@pytest.fixture(autouse=True)
def _fresh_singletons():
    """These are process-wide, so a test must not inherit another's loop."""
    DownloadManager._instance = None
    DownloadBroadcaster._instance = None
    yield
    DownloadManager._instance = None
    DownloadBroadcaster._instance = None


def _add(manager: DownloadManager, item_id: str):
    return manager.add_to_queue(
        item_id=item_id,
        track_name="Title",
        artist_name="Artist",
        album_name="Album",
        spotify_id=item_id,
    )


async def _contend(manager: DownloadManager, tag: str) -> None:
    """One queue write that has to *wait* for the lock.

    Contention is the whole point. asyncio.Lock.acquire() takes an
    uncontended lock without ever calling _get_loop(), so a queue whose
    critical sections are synchronous — as these all are — can be written
    from a dozen loops and never notice. The crash needs a waiter, which is
    what the reported traceback shows: "[unlocked, waiters:1]", raised from
    `fut = self._get_loop().create_future()`.
    """
    async with manager._lock:
        waiting = asyncio.create_task(_add(manager, tag))
        await asyncio.sleep(0)  # let it reach acquire() and queue up
    await waiting


def test_the_queue_survives_a_second_event_loop() -> None:
    """The reported crash: the loop that bound the lock is gone, and the
    next batch brings its own.
    """
    manager = DownloadManager()

    asyncio.run(_contend(manager, "first"))
    asyncio.run(_contend(manager, "second"))  # this is where it raised

    assert [item.id for item in manager._queue] == ["first", "second"]


def test_the_queue_survives_a_long_lived_loop_then_a_batch() -> None:
    """The shape the Docker report actually had.

    The GUI and web bridges go through loop_runner's shared loop; the batch
    that follows brings its own via client.SpotiFLAC()'s asyncio.run().
    """
    manager = DownloadManager()

    from SpotiFLAC.core.loop_runner import run_sync

    run_sync(_contend(manager, "on-the-shared-loop"))
    asyncio.run(_contend(manager, "on-the-batch-loop"))

    assert [item.id for item in manager._queue] == [
        "on-the-shared-loop",
        "on-the-batch-loop",
    ]


def test_the_broadcaster_survives_it_too() -> None:
    """Same singleton pattern, same lock, same crash."""
    broadcaster = DownloadBroadcaster()

    async def _contend_broadcaster() -> None:
        queue: asyncio.Queue = asyncio.Queue()
        async with broadcaster._lock:
            waiting = asyncio.create_task(broadcaster.subscribe(queue))
            await asyncio.sleep(0)
        await waiting

    asyncio.run(_contend_broadcaster())
    asyncio.run(_contend_broadcaster())


def test_the_lock_still_serialises() -> None:
    """Loop-agnostic must not mean unlocked. Each waiter has to take its
    turn: the queue ends up with every write, none lost to a race.
    """
    manager = DownloadManager()

    async def _many() -> None:
        async with manager._lock:
            waiting = [asyncio.create_task(_add(manager, f"t{n}")) for n in range(50)]
            await asyncio.sleep(0)
        await asyncio.gather(*waiting)

    asyncio.run(_many())
    assert len(manager._queue) == 50
    assert len({item.id for item in manager._queue}) == 50

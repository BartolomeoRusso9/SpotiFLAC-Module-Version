"""What happens to the progress worker when the event loop is replaced.

ProgressManager is a process-wide singleton, but the GUI does not run one
long-lived loop: every download goes through its own ``asyncio.run()``. An
``asyncio.Queue`` latches onto the loop that first awaits it, so the queue
built during the first download outlived its loop and every later run hit

    RuntimeError: <Queue ...> is bound to a different event loop

once per reported chunk — a wall of tracebacks, and a "Critical error during
execution" at the end of a download that had actually succeeded.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.progress import ProgressManager


@pytest.fixture(autouse=True)
def _no_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tqdm out of it; this is about the queue, not the drawing."""
    monkeypatch.setenv("SPOTIFLAC_PROGRESS_BARS", "0")
    ProgressManager._event_queue = None
    ProgressManager._queue_loop = None
    ProgressManager._worker_task = None
    yield
    ProgressManager._event_queue = None
    ProgressManager._queue_loop = None
    ProgressManager._worker_task = None


async def _one_download(name: str) -> None:
    ProgressManager.enqueue_progress(name, name, 10, 100)
    ProgressManager.enqueue_progress(name, name, 100, 100)
    await asyncio.sleep(0)
    await ProgressManager.clear_all()


def test_a_second_download_in_a_fresh_loop_does_not_reuse_the_dead_queue() -> None:
    # Three separate asyncio.run() calls is exactly what the GUI does.
    for n in range(3):
        asyncio.run(_one_download(f"track-{n}"))


def test_the_queue_is_rebuilt_when_the_loop_changes() -> None:
    seen: list[object] = []

    async def _capture() -> None:
        ProgressManager.start_worker()
        seen.append(ProgressManager._event_queue)
        await ProgressManager.clear_all()

    asyncio.run(_capture())
    asyncio.run(_capture())

    first, second = seen
    assert first is not None
    # Same object across loops is the bug; a fresh queue is the fix.
    assert first is not second


def test_the_queue_is_kept_within_one_loop() -> None:
    seen: list[object] = []

    async def _capture_twice() -> None:
        ProgressManager.start_worker()
        seen.append(ProgressManager._event_queue)
        ProgressManager.start_worker()
        seen.append(ProgressManager._event_queue)
        await ProgressManager.clear_all()

    asyncio.run(_capture_twice())
    assert seen[0] is seen[1]


def test_starting_the_worker_without_a_loop_is_a_no_op() -> None:
    # enqueue_progress() calls start_worker() unconditionally; off-loop it
    # must drop the event, not raise into whatever reported the bytes.
    ProgressManager.start_worker()
    assert ProgressManager._worker_task is None
    ProgressManager.enqueue_progress("x", "x", 1, 2)

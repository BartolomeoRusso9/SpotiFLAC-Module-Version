"""Process-wide locks have to survive more than one event loop.

Reported from a --web run, at the start of an album:

    [isrc] bulk resolution async failed: <asyncio.locks.Lock object ...
    [locked]> is bound to a different event loop

Each --web download is its own asyncio.run(). A module-level asyncio.Lock
binds to the first loop that has to wait on it, so the first batch works and
a later one crashes as soon as two lookups contend. The same pattern sat in
every module here; they now share core/cross_loop_lock.CrossLoopLock.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from SpotiFLAC.core.cross_loop_lock import CrossLoopLock


async def _contend(lock) -> None:
    """One acquire that has to *wait*: uncontended ones never touch the loop."""
    order: list[str] = []

    async def _second() -> None:
        async with lock:
            order.append("second")

    async with lock:
        waiter = asyncio.create_task(_second())
        await asyncio.sleep(0)
        order.append("first")
    await waiter
    assert order == ["first", "second"]


def _shared_locks():
    from SpotiFLAC.core import dns_doh, isrc_cache, lyrics, profiles, session_memory
    from SpotiFLAC.core.progress import DownloadManager
    from SpotiFLAC.core.provider_stats import ProviderScorer

    scorer = ProviderScorer()
    return {
        "isrc_cache": isrc_cache._cache_lock,
        "dns_doh": dns_doh._cache_lock,
        "lyrics": lyrics._spotify_token_lock,
        "profiles": profiles._io_lock,
        "session_memory": session_memory._io_lock,
        "provider_stats._stats_lock": scorer._stats_lock,
        "provider_stats._init_lock": scorer._init_lock,
        "progress": DownloadManager()._lock,
    }


@pytest.mark.parametrize("name", sorted(_shared_locks()))
def test_contended_from_two_loops(name) -> None:
    lock = _shared_locks()[name]
    assert isinstance(lock, CrossLoopLock), name
    asyncio.run(_contend(lock))
    asyncio.run(_contend(lock))  # raised "bound to a different event loop"


def test_excludes_threads_too() -> None:
    """Two downloads on two threads, each with its own loop."""
    lock = CrossLoopLock(poll_interval=0.001)
    inside = 0
    overlap = False

    async def _worker() -> None:
        nonlocal inside, overlap
        for _ in range(50):
            async with lock:
                inside += 1
                overlap = overlap or inside > 1
                await asyncio.sleep(0)
                inside -= 1

    threads = [
        threading.Thread(target=asyncio.run, args=(_worker(),)) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap


def test_isrc_cache_across_batches(tmp_path, monkeypatch) -> None:
    """The reported path itself: two batches, each its own asyncio.run()."""
    from SpotiFLAC.core import isrc_cache

    monkeypatch.setattr(isrc_cache, "_CACHE_FILE", tmp_path / "isrc-cache.json")
    monkeypatch.setattr(isrc_cache, "_cache", None)

    async def _batch(tag: str) -> list[str]:
        await asyncio.gather(
            *(
                isrc_cache.put_cached_isrc_async(f"{tag}{i}", f"us{i:010d}")
                for i in range(20)
            )
        )
        return await asyncio.gather(
            *(isrc_cache.get_cached_isrc_async(f"{tag}{i}") for i in range(20))
        )

    assert asyncio.run(_batch("a"))[3] == "US0000000003"
    assert asyncio.run(_batch("b"))[7] == "US0000000007"

"""SpotiFLAC/core/output_lock.py — one writer per output file.

Two downloads can resolve to the same destination. A playlist that lists a
track twice, an album and the single it came from queued together, the same
track picked from two sources — all end at one path, because the filename
comes from the metadata and identical metadata gives an identical name.

Nothing stopped them writing it at the same time. Both stream into the same
`.part`, both rename over the same destination, and the file that survives
is whichever finished last, its bytes possibly interleaved with the other's.
It is silent, it needs two downloads to collide on one name, and the result
is a corrupt file that looks complete — the combination that makes it worth
a lock rather than a comment.

Serialising by *path* rather than globally is the point: unrelated downloads
keep running in parallel, and only a genuine collision waits.

The lock has two halves, and it needs both. The GUI runs each API call in
its own thread with its own asyncio.run(), so an asyncio.Lock cached once
and awaited under a second loop is exactly the cross-loop reuse asyncio
warns about — that is why AsyncRateLimiter in core/http.py keys its locks by
loop, and why the asyncio.Lock here is per loop too.

But per-loop is only half an answer for this particular job. Two downloads
colliding on one filename are exactly as likely to be running under two
loops as under one, and against two loops a per-loop lock is not a lock at
all: each thread takes its own, both proceed, and the corruption the module
exists to prevent happens anyway. So the per-loop asyncio.Lock serialises
the coroutines inside one loop, and a plain threading.Lock underneath it
serialises the loops against each other. Taking the asyncio half first
means at most one thread per loop is ever parked in the executor waiting on
the threading half.
"""

from __future__ import annotations

import asyncio
import os
import threading
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class _PathLock:
    """One destination's coordinator: one cross-loop gate, one gate per loop.

    `across_loops` is what actually guarantees a single writer. `in_loop` is
    a per-loop fast path so that concurrent coroutines under one loop queue
    on an asyncio primitive instead of each occupying an executor thread.
    """

    __slots__ = ("across_loops", "in_loop")

    def __init__(self) -> None:
        self.across_loops = threading.Lock()
        #: A weak map, so a lock dies with the loop that owns it.
        self.in_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = weakref.WeakKeyDictionary()

    def for_current_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self.in_loop.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self.in_loop[loop] = lock
        return lock


#: path key -> the one coordinator for that destination, guarded by
#: _registry_lock. One per path, never one per (path, loop): see the module
#: docstring for why the cross-loop half cannot be skipped.
_locks: dict[str, _PathLock] = {}
_registry_lock = threading.Lock()


def _key(path: str | Path) -> str:
    """The identity of a destination.

    Normalised and case-folded because macOS and Windows treat
    "Artist/Song.flac" and "artist/song.FLAC" as one file, and a lock that
    did not would let exactly the collision it exists to prevent through.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def _lock_for(path: str | Path) -> _PathLock:
    key = _key(path)
    with _registry_lock:
        coordinator = _locks.get(key)
        if coordinator is None:
            coordinator = _PathLock()
            _locks[key] = coordinator
        return coordinator


@asynccontextmanager
async def output_path_lock(path: str | Path) -> AsyncIterator[None]:
    """Holds the right to write `path` for the duration of the block.

    A second download of the same destination waits here rather than
    interleaving with the first — whether it runs in this event loop or in
    another thread's. Every other path proceeds untouched.
    """
    coordinator = _lock_for(path)
    async with coordinator.for_current_loop():
        # Uncontended in the overwhelming majority of cases, so try without
        # paying for an executor thread first.
        acquired = coordinator.across_loops.acquire(blocking=False)
        if not acquired:
            await asyncio.to_thread(coordinator.across_loops.acquire)
        try:
            yield
        finally:
            coordinator.across_loops.release()


def tracked_paths() -> int:
    """How many destinations have been locked. For tests and diagnostics."""
    with _registry_lock:
        return len(_locks)

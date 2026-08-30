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

The multi-loop shape mirrors core/http.py's AsyncRateLimiter, for the same
reason given there: the GUI runs each API call in its own thread with its
own asyncio.run(), so an asyncio.Lock cached once and awaited under a second
loop is exactly the cross-loop reuse asyncio warns about.
"""

from __future__ import annotations

import asyncio
import os
import threading
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

#: path key -> {loop -> lock}. The outer dict is guarded by _registry_lock;
#: the inner one is a weak map so a lock dies with the loop that owns it.
_locks: dict[
    str,
    weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock],
] = {}
_registry_lock = threading.Lock()


def _key(path: str | Path) -> str:
    """The identity of a destination.

    Normalised and case-folded because macOS and Windows treat
    "Artist/Song.flac" and "artist/song.FLAC" as one file, and a lock that
    did not would let exactly the collision it exists to prevent through.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def _lock_for(path: str | Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = _key(path)
    with _registry_lock:
        per_loop = _locks.get(key)
        if per_loop is None:
            per_loop = weakref.WeakKeyDictionary()
            _locks[key] = per_loop
        lock = per_loop.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[loop] = lock
        return lock


@asynccontextmanager
async def output_path_lock(path: str | Path) -> AsyncIterator[None]:
    """Holds the right to write `path` for the duration of the block.

    A second download of the same destination waits here rather than
    interleaving with the first. Every other path proceeds untouched.
    """
    async with _lock_for(path):
        yield


def tracked_paths() -> int:
    """How many destinations have been locked. For tests and diagnostics."""
    with _registry_lock:
        return len(_locks)

"""SpotiFLAC/core/cross_loop_lock.py — a lock `async with` can use from any loop.

An ``asyncio.Lock`` binds to the first event loop that has to *wait* on it
and raises on every other one:

    RuntimeError: <asyncio.locks.Lock ...> is bound to a different event loop

This process runs many loops. ``core/loop_runner.py`` owns a long-lived one
for the GUI and web bridges, ``client.SpotiFLAC()`` keeps its own
``asyncio.run()`` per batch, and under ``--web`` each background download is
its own ``asyncio.run()`` on its own thread. A module-level ``asyncio.Lock``
therefore works for the first batch and crashes the first time two later ones
contend. Uncontended acquires never touch the loop, which is why it only
shows up under load.

A ``threading.Lock`` has no loop affinity. It is taken without blocking, and
the caller yields while it is busy, so the loop is never held up. It also
serialises *threads*, which the asyncio lock never did: two downloads on two
threads writing the same cache file were not actually excluded by it.

Holding it across an ``await`` is allowed — waiters just poll a little longer
— but keep what it guards short.
"""

from __future__ import annotations

import asyncio
import threading


class CrossLoopLock:
    """``async with`` mutual exclusion across event loops and threads."""

    def __init__(self, poll_interval: float = 0.01) -> None:
        self._lock = threading.Lock()
        self._poll_interval = poll_interval

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> CrossLoopLock:
        # A plain yield first: an uncontended lock is the overwhelmingly
        # common case, and a contended one is usually free again within a
        # single tick, so the poll interval should be the exception.
        if not self._lock.acquire(blocking=False):
            await asyncio.sleep(0)
            while not self._lock.acquire(blocking=False):
                await asyncio.sleep(self._poll_interval)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._lock.release()
        return False

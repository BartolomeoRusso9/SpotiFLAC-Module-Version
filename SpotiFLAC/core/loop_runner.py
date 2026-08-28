"""One long-lived event loop for the synchronous bridges.

The problem this solves
-----------------------
The GUI/web API object (app.py and its mixins) is synchronous: pywebview
calls its methods directly, and webapp.py runs them in a threadpool. The
work underneath is async. The bridge between the two used to be
`asyncio.run()` — 32 separate call sites, 12 in app.py alone.

`asyncio.run()` creates a loop, runs one coroutine, and closes it. Doing
that per call means:

  - Every call pays a fresh TLS handshake. NetworkManager keys its
    httpx.AsyncClient by loop, so a new loop is always a cache miss; the
    "global connection pooling" in that module's docstring never happened
    in GUI or web mode.
  - Anything with loop-bound state and a process-wide lifetime is caught
    between loops — the rate limiters in core/http.py being the clearest
    case, since their whole job is to remember what happened before.
  - Loops are created and destroyed constantly under a UI that is otherwise
    idle.

What this does
--------------
`run_coro(coro)` submits to a single daemon-thread loop that lives for the
process, and blocks until the result is ready. Same signature shape as
`asyncio.run()` from the caller's side, so migrating a call site is a
one-line change, but every call now shares one loop — and therefore one
connection pool, one set of rate-limiter clocks, one set of caches.

Deliberately not a replacement for `asyncio.run()` everywhere: `client.py`'s
`SpotiFLAC()` wrapper still owns its own loop for a single batch run, which
is correct — it is a one-shot entry point that should leave nothing behind.
This is for the long-lived, many-small-calls case.

Reentrancy
----------
`run_coro()` blocks the calling thread, so calling it *from* the shared loop
would deadlock. It raises RuntimeError instead of hanging if you try.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class _SharedLoop:
    """Owns the background thread and its event loop, created on first use."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            return loop

        with self._lock:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                return loop
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="spotiflac-loop",
                daemon=True,
            )
            self._thread.start()
            # Wait for _run() to publish the loop; without this a caller can
            # race ahead and read self._loop while it is still None.
            self._ready.wait(timeout=10)
            if self._loop is None:
                msg = "Background event loop failed to start"
                raise RuntimeError(msg)
            return self._loop

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                _cancel_pending(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def is_current(self) -> bool:
        """Whether the caller is already running *on* the shared loop."""
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or loop.is_closed():
            return
        # Close the shared httpx client on the loop that owns it, before the
        # loop stops — otherwise its connections are dropped rather than
        # closed, and asyncio complains about pending transports at exit.
        with _suppressed():
            fut = asyncio.run_coroutine_threadsafe(_aclose_network(), loop)
            fut.result(timeout=timeout)
        with _suppressed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)


def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(
            asyncio.gather(*pending, return_exceptions=True),
        )


async def _aclose_network() -> None:
    from .http import NetworkManager

    await NetworkManager.aclose_loop_client()


class _suppressed:
    """contextlib.suppress(Exception), spelled out to keep this module's
    import list to the standard library essentials it already needs.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


_shared = _SharedLoop()


def get_loop() -> asyncio.AbstractEventLoop:
    """The shared loop, starting its thread on first call."""
    return _shared.loop()


def run_coro(coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
    """Runs `coro` on the shared loop and returns its result.

    Drop-in for `asyncio.run(coro)` in synchronous bridge code. Exceptions
    propagate to the caller unchanged, so `try/except` around a converted
    call site keeps working.
    """
    if _shared.is_current():
        coro.close()
        msg = (
            "run_coro() called from inside the shared loop — this would "
            "deadlock. Await the coroutine directly instead."
        )
        raise RuntimeError(msg)

    future = asyncio.run_coroutine_threadsafe(coro, get_loop())
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise


def run_sync(coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
    """Runs `coro` from synchronous code that may itself be called from
    anywhere — including from inside a running event loop.

    Replaces the `_run_async_sync` helper that was copy-pasted into
    core/spotify_metadata.py, core/metadata_enrichment.py, core/musicbrainz.py
    and core/progress.py. Each copy did the same three-way dance: no running
    loop → `asyncio.run()`; a running loop → hand the coroutine to a
    ThreadPoolExecutor that called `asyncio.run()` on a *third* loop; a
    non-running loop → `run_until_complete`.

    Every one of those branches created a loop, and therefore lost the
    connection pool and every other piece of loop-bound state. Here only the
    genuinely impossible case — being called from the shared loop itself,
    where blocking would deadlock — still needs a throwaway loop.
    """
    if _shared.is_current():
        return _run_in_worker(coro, timeout)
    return run_coro(coro, timeout)


def _run_in_worker(coro: Coroutine[Any, Any, T], timeout: float | None) -> T:
    """Last resort: a private loop on a worker thread, for a caller already
    running on the shared loop.
    """
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="spotiflac-nested"
    ) as executor:
        return executor.submit(asyncio.run, coro).result(timeout=timeout)


def submit(coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
    """Fire-and-forget (or poll-later) variant: schedules `coro` and returns
    immediately with a Future. For work the UI shouldn't block on.
    """
    return asyncio.run_coroutine_threadsafe(coro, get_loop())


def shutdown(timeout: float = 5.0) -> None:
    """Stops the shared loop, closing the pooled HTTP client first.

    Registered with atexit; also useful in tests that want a clean slate.
    Safe to call when nothing was ever started.
    """
    _shared.shutdown(timeout=timeout)


atexit.register(shutdown)

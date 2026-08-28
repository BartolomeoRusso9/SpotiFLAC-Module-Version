"""Centralized HTTP client with global connection pooling.

=== Phase 3 — async migration complete ===
Removed all sync code (RateLimiter, HttpClient, NetworkManager.get_sync_client,
legacy NetworkManager.get_async_client) now that every provider uses AsyncHttpClient.
"""

from __future__ import annotations

import asyncio
import atexit as _atexit
import contextlib
import logging
import os
import re
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)

from .errors import (
    AuthError,
    NetworkError,
    ParseError,
    RateLimitedError,
    TrackNotFoundError,
)

try:
    import aiofiles
except ImportError:
    aiofiles = None


class _RedactUrlFilter(logging.Filter):
    _url_re = re.compile(r"https?://\S+")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._url_re.sub("[endpoint]", record.getMessage())
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(_RedactUrlFilter())

logger = logging.getLogger(__name__)


def _remove_quietly(path: str) -> None:
    """Deletes `path` if present, never raising — used on cleanup paths that
    are already unwinding an exception and must not mask it with a second one.
    """
    with contextlib.suppress(OSError):
        os.remove(path)


# --- CONNECTION POOL MANAGER ---
class NetworkManager:
    """Keeps connections alive (Keep-Alive) to eliminate SSL handshake time.
    Each event loop gets its own httpx.AsyncClient instance (loop-safe).

    Keyed by the loop *object*, in a WeakKeyDictionary, rather than by
    id(loop). Two reasons, and the first is a correctness bug rather than
    housekeeping:

      - CPython reuses the memory address of a collected object, so
        successive asyncio.run() calls hand out colliding ids — in a tight
        loop, almost always (measured: 197 collisions in 200 runs). An
        id-keyed registry therefore returns a client bound to an
        already-closed loop to a brand-new one, which fails later and
        intermittently, wherever the pool first touches loop-bound state.
      - The entry disappears with the loop instead of accumulating one dead
        client per asyncio.run() for the life of the process.

    Note this restores correctness, not the pooling itself: a caller that
    opens a fresh loop per call still gets a fresh client, and pays for a
    fresh TLS handshake. Reusing one long-lived loop is what makes the
    keep-alive above do anything.
    """

    _async_clients: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop, httpx.AsyncClient
    ] = weakref.WeakKeyDictionary()
    _async_clients_lock = threading.Lock()

    @classmethod
    async def get_async_client_safe(cls) -> httpx.AsyncClient:
        """Returns an AsyncClient tied to the current loop.
        Creates a new client if the loop does not already have one.
        """
        loop = asyncio.get_running_loop()

        # Fast path without a lock for the common case (client already exists)
        client = cls._async_clients.get(loop)
        if client is not None:
            return client

        with cls._async_clients_lock:
            client = cls._async_clients.get(loop)
            if client is None:
                limits = httpx.Limits(max_keepalive_connections=30, max_connections=100)
                # http2 is what the httpx[http2] dependency is for; it was
                # never switched on, so the h2 package was being installed and
                # not used. Negotiated over ALPN, so servers that don't speak
                # HTTP/2 transparently stay on 1.1.
                client = httpx.AsyncClient(limits=limits, timeout=30.0, http2=True)
                cls._async_clients[loop] = client
        return client

    @classmethod
    async def aclose_loop_client(cls) -> None:
        """Closes and removes the current loop's async client from the registry."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with cls._async_clients_lock:
            client = cls._async_clients.pop(loop, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    @classmethod
    def close(cls) -> None:
        """Best-effort cleanup of async clients at process exit (called by atexit).
        Loops may already be closed: we limit ourselves to clearing the registry.
        """
        try:
            with cls._async_clients_lock:
                cls._async_clients.clear()
        except Exception:
            pass


# --- RATE LIMITER ASINCRONO ---
class AsyncRateLimiter:
    """Sliding-window rate limiter, safe across both loops and threads.

    The instances that matter are module-level singletons (see below), and
    the GUI runs each API call in its own thread with its own asyncio.run()
    — so "one limiter, one loop, one thread" is exactly the situation this
    class is never in. Three things follow from that:

      - The window is protected by a threading.Lock, not only an
        asyncio.Lock. An asyncio.Lock serialises coroutines within a single
        loop and offers no protection at all against a second thread
        mutating the same deque.
      - Timestamps come from time.monotonic(), not loop.time(). Loop clocks
        have no defined relationship to each other, so comparing a
        timestamp recorded under one loop against `now` from another was
        meaningless.
      - The asyncio.Lock is per-loop. A single cached lock, first awaited
        under one loop and then reused under another, is precisely the
        cross-loop reuse asyncio warns about.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self.timestamps: deque = deque()
        # Guards `timestamps`. Only ever held for a few statements, never
        # across an await, so it cannot block the event loop.
        self._state_lock = threading.Lock()
        self._loop_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = weakref.WeakKeyDictionary()

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._loop_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._loop_locks[loop] = lock
        return lock

    async def wait_for_slot(self) -> None:
        # Held across the sleep on purpose: it lets one waiter per loop
        # re-check at a time. The previous version released the lock, slept,
        # then appended unconditionally — so N coroutines that had all queued
        # up woke together and every one of them took a slot, overshooting
        # max_requests exactly when the limit was already binding.
        async with self._get_lock():
            while True:
                with self._state_lock:
                    now = time.monotonic()
                    cutoff = now - self.window
                    while self.timestamps and self.timestamps[0] <= cutoff:
                        self.timestamps.popleft()
                    if len(self.timestamps) < self.max_requests:
                        self.timestamps.append(now)
                        return
                    wait_duration = (self.timestamps[0] + self.window) - now

                await asyncio.sleep(max(wait_duration, 0.0))


# Rate limiters globali async
async_zarz_rate_limiter = AsyncRateLimiter(5, 10.0)
async_songlink_rate_limiter = AsyncRateLimiter(9, 60.0)


@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    backoff_factor: float = 2.0


# --- HTTP CLIENT ASINCRONO ---
class AsyncHttpClient:
    """Single HTTP client used by every provider.
    Uses NetworkManager.get_async_client_safe() for multi-loop safety.
    """

    def __init__(
        self,
        provider: str,
        timeout_s: int = 30,
        rate_limiter: AsyncRateLimiter | None = None,
        headers: dict[str, str] | None = None,
        retry: RetryConfig | None = None,
    ) -> None:
        self._provider = provider
        self._timeout = timeout_s
        self._limiter = rate_limiter
        self._headers = headers or {}
        self._retry = retry or RetryConfig()
        self._stop_event: asyncio.Event | None = None

    async def _client(self) -> httpx.AsyncClient:
        return await NetworkManager.get_async_client_safe()

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def get_json_async(self, url: str, **kwargs: Any) -> dict:
        resp = await self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError:
            raise ParseError(self._provider, "Invalid JSON")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        req_timeout = kwargs.pop("timeout", self._timeout)

        async def _attempt() -> httpx.Response:
            if self._limiter:
                await self._limiter.wait_for_slot()
            client = await self._client()
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    timeout=req_timeout,
                    **kwargs,
                )
            except httpx.TransportError as exc:
                raise NetworkError(self._provider, f"Request failed: {exc}") from exc
            self._raise_for_status(resp)
            return resp

        retryer = AsyncRetrying(
            stop=stop_after_attempt(self._retry.max_attempts),
            retry=retry_if_exception_type((RateLimitedError, NetworkError)),
            wait=self._wait_strategy,
            reraise=True,
        )
        return await retryer(_attempt)

    def _wait_strategy(self, retry_state: RetryCallState) -> float:
        """Retry-After for 429s; otherwise exponential backoff from RetryConfig."""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, RateLimitedError):
            return min(exc.retry_after, self._retry.max_delay_s)
        delay = self._retry.base_delay_s * (
            self._retry.backoff_factor ** (retry_state.attempt_number - 1)
        )
        return min(delay, self._retry.max_delay_s)

    def _raise_for_status(self, resp: httpx.Response) -> None:
        sc = resp.status_code
        if sc == 200:
            return
        if sc == 401:
            raise AuthError(self._provider, "Unauthorized (401)")
        if sc == 403:
            raise AuthError(self._provider, "Forbidden (403)")
        if sc == 404:
            raise TrackNotFoundError(self._provider, str(resp.url))
        if sc == 429:
            raise RateLimitedError(
                self._provider,
                int(resp.headers.get("Retry-After", 5)),
            )
        if not resp.is_success:
            raise NetworkError(self._provider, f"HTTP {sc} from {resp.url}")

    async def stream_to_file(
        self,
        url: str,
        dest_path: str,
        progress_cb: Any = None,
        chunk_size: int = 256 * 1024,
        extra_headers: dict | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        if aiofiles is None:
            msg = (
                "aiofiles non installato — richiesto da AsyncHttpClient.stream_to_file(). "
                "Eseguire: pip install aiofiles"
            )
            raise RuntimeError(
                msg,
            )

        temp = dest_path + ".part"
        headers = extra_headers or {}
        if self._limiter:
            await self._limiter.wait_for_slot()

        client = await self._client()

        try:
            async with client.stream(
                "GET",
                url,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                self._raise_for_status(resp)
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

                evt = stop_event or self._stop_event

                async with aiofiles.open(temp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size):
                        if evt is not None and evt.is_set():
                            raise NetworkError(
                                self._provider,
                                "Stream cancelled by stop_event",
                            )
                        if not chunk:
                            continue
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)

            os.replace(temp, dest_path)

        except httpx.RequestError as exc:
            _remove_quietly(temp)
            raise NetworkError(self._provider, f"Stream failed: {exc}") from exc
        except BaseException:
            # BaseException, not Exception: cancelling a download raises
            # asyncio.CancelledError, which since 3.8 does NOT derive from
            # Exception. The narrower `except (OSError, NetworkError)` here
            # meant every cancelled download — the stop button, a timeout, a
            # gather() tearing down its siblings — left its .part file behind
            # to accumulate in the output folder.
            _remove_quietly(temp)
            raise


_atexit.register(NetworkManager.close)

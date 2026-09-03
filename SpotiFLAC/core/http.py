"""Centralized HTTP client with global connection pooling.

=== Phase 3 — async migration complete ===
Removed all sync code (RateLimiter, HttpClient, NetworkManager.get_sync_client,
legacy NetworkManager.get_async_client) now that every provider uses AsyncHttpClient.
"""

from __future__ import annotations

import asyncio
import atexit as _atexit
import contextlib
import ipaddress
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
    """Replaces URLs in httpx's own log records with `[endpoint]`.

    httpx logs the full request URL at INFO, and provider URLs here routinely
    carry tokens and signed query strings — that is what the CodeQL
    "clear-text logging of sensitive information" finding was about. Redacting
    at the source keeps them out of terminals, log files and pasted bug
    reports alike.
    """

    _url_re = re.compile(r"https?://\S+")

    def filter(self, record: logging.LogRecord) -> bool:
        # Destructive by necessity: a filter cannot hand a *copy* downstream,
        # and leaving the original intact would defeat the point the moment
        # another handler formats it.
        record.msg = self._url_re.sub("[endpoint]", record.getMessage())
        record.args = ()
        return True


REDACTION_DISABLE_ENV = "SPOTIFLAC_NO_LOG_REDACTION"

_redaction_filter: _RedactUrlFilter | None = None


def install_log_redaction(force: bool = False) -> bool:
    """Attaches the URL-redacting filter to httpx's logger. Idempotent.

    Called once at import, which is a real liberty for a library to take with
    someone else's logging config — so it is at least named, documented,
    reversible (`remove_log_redaction()`), and skippable by setting
    $SPOTIFLAC_NO_LOG_REDACTION. It stays on by default anyway because the
    alternative default is "leak provider tokens into the user's logs", and
    a security control that has to be switched on is not one most people get.

    `force=True` ignores the environment variable. Returns whether the filter
    is attached afterwards.
    """
    global _redaction_filter
    if not force and os.environ.get(REDACTION_DISABLE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    ):
        return False
    if _redaction_filter is None:
        _redaction_filter = _RedactUrlFilter()
        logging.getLogger("httpx").addFilter(_redaction_filter)
    return True


def remove_log_redaction() -> None:
    """Detaches the filter — for an application that does its own redaction,
    or a test that needs to assert on a real URL.
    """
    global _redaction_filter
    if _redaction_filter is not None:
        logging.getLogger("httpx").removeFilter(_redaction_filter)
        _redaction_filter = None


install_log_redaction()

logger = logging.getLogger(__name__)


# httpx's default is "gzip, deflate, br, zstd" whenever the `zstandard`
# package is importable. We drop zstd on purpose.
#
# httpx 0.28.1's ZStandardDecoder mishandles a multi-frame zstd body when a
# frame ends exactly on a network chunk boundary: it only swaps in a fresh
# decompressor while `unused_data` is non-empty, so a frame that ends with
# the chunk leaves the exhausted decompressobj in place, and the next chunk
# fails with `DecodingError: cannot use a decompressobj multiple times`.
#
# Whether that happens depends on how the body happens to be split across
# packets, which is why it showed up as one random failure in a batch of
# ~1800 metadata fetches rather than as a reproducible one. Not advertising
# zstd means servers negotiate br/gzip instead and the broken path is
# unreachable; the bodies here are small JSON documents, so the compression
# difference is not worth the flakiness.
#
# Revisit when the pinned httpx carries the upstream fix.
#
# `br` is not listed either, for a plainer reason: httpx only decodes Brotli
# when `brotli`/`brotlicffi` is installed, and neither is a declared
# dependency — `httpx[http2]` does not pull one in. Advertising it anyway
# invites a server to send a body nothing here can decode. Add it back the
# day Brotli support becomes a required dependency, not before.
SAFE_ACCEPT_ENCODING = "gzip, deflate"


_CONTENT_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(?:\d+|\*)\s*$", re.IGNORECASE)


def _content_range_starts_at(resp: httpx.Response, expected_start: int) -> bool:
    """Whether a 206's Content-Range really begins where we asked.

    A missing or unparseable header is treated as "no" — restarting costs
    one wasted download, appending to the wrong offset costs a silently
    corrupt file.
    """
    header = resp.headers.get("Content-Range", "")
    match = _CONTENT_RANGE_RE.match(header)
    if not match:
        return False
    return int(match.group(1)) == expected_start


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
                client = httpx.AsyncClient(
                    limits=limits,
                    timeout=30.0,
                    http2=True,
                    headers={"Accept-Encoding": SAFE_ACCEPT_ENCODING},
                )
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
                resp = await self._retry_over_doh(
                    method, url, headers, req_timeout, exc, kwargs
                )
                if resp is None:
                    raise NetworkError(
                        self._provider, f"Request failed: {exc}"
                    ) from exc
            self._raise_for_status(resp)
            return resp

        retryer = AsyncRetrying(
            stop=stop_after_attempt(self._retry.max_attempts),
            retry=retry_if_exception_type((RateLimitedError, NetworkError)),
            wait=self._wait_strategy,
            reraise=True,
        )
        return await retryer(_attempt)

    async def _retry_over_doh(
        self,
        method: str,
        url: str,
        headers: dict,
        timeout: Any,
        exc: BaseException,
        kwargs: dict,
    ) -> httpx.Response | None:
        """One retry against an address resolved over DNS-over-HTTPS.

        Only for a failure that was specifically the *name* not resolving,
        which is how DNS-level ISP blocking presents. A refused connection
        or a timeout is a different problem and retrying it here would just
        double the cost of every ordinary outage.

        The request keeps its original Host header and SNI, so TLS is still
        verified against the hostname the caller asked for: a resolver
        handing back somebody else's address fails the handshake rather than
        silently receiving the traffic.
        """
        from urllib.parse import urlsplit, urlunsplit

        from .dns_doh import looks_like_dns_failure, resolve_async

        if not looks_like_dns_failure(exc):
            return None

        parts = urlsplit(url)
        hostname = parts.hostname or ""
        if not hostname:
            return None
        with contextlib.suppress(ValueError):
            ipaddress.ip_address(hostname)
            return None  # already an address; DNS was never involved

        for address in await resolve_async(hostname):
            netloc = f"[{address}]" if ":" in address else address
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            try:
                client = await self._client()
                resp = await client.request(
                    method,
                    urlunsplit(parts._replace(netloc=netloc)),
                    headers={**headers, "Host": parts.netloc},
                    timeout=timeout,
                    extensions={"sni_hostname": hostname},
                    **kwargs,
                )
            except Exception as retry_exc:
                logger.debug(
                    "[doh] retry via %s failed for %s: %s",
                    address,
                    hostname,
                    retry_exc,
                )
                continue
            logger.info(
                "[doh] %s resolved via DNS-over-HTTPS after the system resolver failed",
                hostname,
            )
            return resp
        return None

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
        resume: bool = True,
    ) -> None:
        """Streams `url` to `dest_path`, via a `.part` file.

        `resume=True` (default) picks up where an interrupted download left
        off: if a `.part` file survives, its size is sent as a `Range: bytes=
        N-` header and the response is appended rather than restarted. On a
        flaky connection, or a discography interrupted halfway, this is the
        difference between losing the last chunk and losing the whole file.

        Servers that ignore Range and answer 200 are handled correctly — the
        partial file is discarded and the download restarts from zero — so
        this is safe against providers with no Range support.
        """
        if aiofiles is None:
            msg = (
                "aiofiles non installato — richiesto da AsyncHttpClient.stream_to_file(). "
                "Eseguire: pip install aiofiles"
            )
            raise RuntimeError(
                msg,
            )

        # Providers hand us whatever their API returned, and some of them
        # return a base64 manifest where a URL was expected. httpx accepts a
        # relative URL and only fails deep inside urllib, with the whole blob
        # in the message — so reject it here, naming the provider and showing
        # just enough of the value to identify it.
        if not str(url).startswith(("http://", "https://")):
            shown = str(url)[:80] + ("…" if len(str(url)) > 80 else "")
            raise NetworkError(
                self._provider,
                f"not an absolute HTTP(S) URL: {shown!r}",
            )

        temp = dest_path + ".part"
        headers = dict(extra_headers or {})

        resume_from = 0
        if resume and "Range" not in headers and "range" not in headers:
            try:
                resume_from = os.path.getsize(temp)
            except OSError:
                resume_from = 0
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"

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
                if resume_from and resp.status_code == 416:
                    # "Range Not Satisfiable": the .part is already at or past
                    # the resource length — almost always a complete file from
                    # a run that died between the last chunk and the rename.
                    # Start over rather than guess; one wasted download beats
                    # a silently truncated FLAC.
                    _remove_quietly(temp)
                    await self.stream_to_file(
                        url,
                        dest_path,
                        progress_cb=progress_cb,
                        chunk_size=chunk_size,
                        extra_headers=extra_headers,
                        stop_event=stop_event,
                        resume=False,
                    )
                    return

                self._raise_for_status(resp)

                # A server free to ignore Range answers 200 with the whole
                # body. Appending that to the partial file would corrupt it —
                # and so would trusting a 206 blindly: the status only says
                # "partial", not "the partial you asked for". A server may
                # clamp the range, ignore the offset, or answer
                # multipart/byteranges, and every one of those appended to a
                # .part file produces a corrupt FLAC that decodes far enough
                # to look fine.
                resuming = (
                    resume_from > 0
                    and resp.status_code == 206
                    and _content_range_starts_at(resp, resume_from)
                    and "multipart" not in resp.headers.get("Content-Type", "").lower()
                )
                if resume_from and not resuming:
                    logger.debug(
                        "[%s] Range not honoured as asked (HTTP %s, "
                        "Content-Range %r); restarting the download",
                        self._provider,
                        resp.status_code,
                        resp.headers.get("Content-Range", ""),
                    )
                    resume_from = 0

                content_length = int(resp.headers.get("Content-Length") or 0)
                # Content-Length on a 206 is the length of *this* slice, so
                # the progress callback needs the whole-resource size added
                # back or the bar restarts from a fraction.
                total = content_length + resume_from if content_length else 0
                downloaded = resume_from
                os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

                evt = stop_event or self._stop_event

                async with aiofiles.open(temp, "ab" if resuming else "wb") as f:
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
            if not resume:
                _remove_quietly(temp)
            raise NetworkError(self._provider, f"Stream failed: {exc}") from exc
        except BaseException:
            # BaseException, not Exception: cancelling a download raises
            # asyncio.CancelledError, which since 3.8 does NOT derive from
            # Exception, so the old `except (OSError, NetworkError)` never
            # ran for the most common interruption of all.
            #
            # What happens next depends on `resume`. With it on, the bytes on
            # disk are exactly what the next attempt needs, so they stay — the
            # end-of-run sweep in downloader._remove_partial_files_async()
            # clears the ones belonging to downloads that did finish. With it
            # off, the old contract holds: leave nothing behind.
            if not resume:
                _remove_quietly(temp)
            raise


_atexit.register(NetworkManager.close)

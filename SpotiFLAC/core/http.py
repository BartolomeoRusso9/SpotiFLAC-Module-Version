"""
HTTP client centralizzato con Connection Pooling globale.

=== FASE 3 — Migrazione async completata ===
Rimosso tutto il codice sync (RateLimiter, HttpClient, NetworkManager.get_sync_client,
NetworkManager.get_async_client legacy) ora che tutti i provider usano AsyncHttpClient.
"""
from __future__ import annotations

import asyncio
import atexit as _atexit
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx
import re

from .errors import (
    AuthError, RateLimitedError, NetworkError,
    ParseError, TrackNotFoundError,
)

try:
    import aiofiles
except ImportError:
    aiofiles = None


class _RedactUrlFilter(logging.Filter):
    _url_re = re.compile(r'https?://\S+')

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._url_re.sub('[endpoint]', record.getMessage())
        record.args = ()
        return True

logging.getLogger("httpx").addFilter(_RedactUrlFilter())

logger = logging.getLogger(__name__)


# --- CONNECTION POOL MANAGER ---
class NetworkManager:
    """
    Mantiene vive le connessioni (Keep-Alive) per azzerare i tempi di handshake SSL.
    Ogni event loop ottiene la propria istanza di httpx.AsyncClient (loop-safe).
    """
    _async_clients: dict[int, httpx.AsyncClient] = {}
    _async_clients_lock = threading.Lock()

    @classmethod
    async def get_async_client_safe(cls) -> httpx.AsyncClient:
        """
        Restituisce un AsyncClient legato al loop corrente.
        Crea un nuovo client se il loop non ne ha ancora uno.
        """
        loop = asyncio.get_running_loop()
        loop_id = id(loop)

        # Fast path senza lock per il caso comune (client già esistente)
        client = cls._async_clients.get(loop_id)
        if client is not None:
            return client

        with cls._async_clients_lock:
            client = cls._async_clients.get(loop_id)
            if client is None:
                limits = httpx.Limits(max_keepalive_connections=30, max_connections=100)
                client = httpx.AsyncClient(limits=limits, timeout=30.0)
                cls._async_clients[loop_id] = client
        return client

    @classmethod
    async def aclose_loop_client(cls) -> None:
        """Chiude e rimuove dal registro il client async del loop corrente."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop_id = id(loop)
        with cls._async_clients_lock:
            client = cls._async_clients.pop(loop_id, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    @classmethod
    def close(cls) -> None:
        """
        Best-effort cleanup dei client async a fine processo (chiamato da atexit).
        I loop potrebbero già essere chiusi: ci limitiamo a svuotare il registro.
        """
        try:
            with cls._async_clients_lock:
                cls._async_clients.clear()
        except Exception:
            pass


# --- RATE LIMITER ASINCRONO ---
class AsyncRateLimiter:
    """
    Rate limiter asyncio-safe.
    Usa asyncio.Lock e asyncio.sleep così non blocca l'event loop.
    L'asyncio.Lock viene creato in modo lazy (alla prima wait_for_slot())
    perché non può essere istanziato fuori da un event loop attivo.
    """
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self.timestamps: deque = deque()
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def wait_for_slot(self) -> None:
        lock = self._get_lock()
        async with lock:
            now = asyncio.get_event_loop().time()
            cutoff = now - self.window
            while self.timestamps and self.timestamps[0] <= cutoff:
                self.timestamps.popleft()
            if len(self.timestamps) < self.max_requests:
                self.timestamps.append(now)
                return
            wait_duration = (self.timestamps[0] + self.window) - now

        if wait_duration > 0:
            await asyncio.sleep(wait_duration)

        async with lock:
            self.timestamps.append(asyncio.get_event_loop().time())


# Rate limiters globali async
async_zarz_rate_limiter     = AsyncRateLimiter(5, 10.0)
async_songlink_rate_limiter = AsyncRateLimiter(9, 60.0)


@dataclass
class RetryConfig:
    max_attempts:   int   = 3
    base_delay_s:   float = 1.0
    max_delay_s:    float = 30.0
    backoff_factor: float = 2.0


# --- HTTP CLIENT ASINCRONO ---
class AsyncHttpClient:
    """
    Unico client HTTP usato da tutti i provider.
    Usa NetworkManager.get_async_client_safe() per sicurezza multi-loop.
    """
    def __init__(
        self,
        provider: str,
        timeout_s: int = 30,
        rate_limiter: AsyncRateLimiter | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._provider = provider
        self._timeout = timeout_s
        self._limiter = rate_limiter
        self._headers = headers or {}
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

        if self._limiter:
            await self._limiter.wait_for_slot()

        client = await self._client()
        resp = await client.request(
            method, url,
            headers=headers,
            timeout=req_timeout,
            **kwargs,
        )
        self._raise_for_status(resp)
        return resp

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
            raise RateLimitedError(self._provider, int(resp.headers.get("Retry-After", 5)))
        if not resp.is_success:
            raise NetworkError(self._provider, f"HTTP {sc} from {resp.url}")

    async def stream_to_file(
        self,
        url: str,
        dest_path: str,
        progress_cb: Any = None,
        chunk_size: int = 256 * 1024,
        extra_headers: dict | None = None,
        stop_event: "asyncio.Event | None" = None,
    ) -> None:
        if aiofiles is None:
            raise RuntimeError(
                "aiofiles non installato — richiesto da AsyncHttpClient.stream_to_file(). "
                "Eseguire: pip install aiofiles"
            )

        temp = dest_path + ".part"
        headers = extra_headers or {}
        if self._limiter:
            await self._limiter.wait_for_slot()

        client = await self._client()

        try:
            async with client.stream("GET", url, headers=headers,
                                      timeout=self._timeout) as resp:
                self._raise_for_status(resp)
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

                evt = stop_event or self._stop_event

                async with aiofiles.open(temp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size):
                        if evt is not None and evt.is_set():
                            raise NetworkError(self._provider, "Stream cancelled by stop_event")
                        if not chunk:
                            continue
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)

            os.replace(temp, dest_path)

        except httpx.RequestError as exc:
            if os.path.exists(temp):
                os.remove(temp)
            raise NetworkError(self._provider, f"Stream failed: {exc}") from exc
        except (OSError, NetworkError):
            if os.path.exists(temp):
                os.remove(temp)
            raise


_atexit.register(NetworkManager.close)
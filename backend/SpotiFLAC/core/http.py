"""
HTTP client centralizzato con Connection Pooling globale e retry esponenziale.
Sostituisce 'requests' con 'httpx' per prestazioni nettamente superiori.
"""
from __future__ import annotations

import logging
import os
import time
import threading
import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import (
    AuthError, RateLimitedError, NetworkError,
    ParseError, TrackNotFoundError, SpotiflacError,
)

logger = logging.getLogger(__name__)

# --- CONNECTION POOL MANAGER ---
class NetworkManager:
    """Mantiene vive le connessioni (Keep-Alive) per azzerare i tempi di handshake SSL."""
    _sync_client: httpx.Client | None = None
    _async_client: httpx.AsyncClient | None = None

    @classmethod
    def get_sync_client(cls) -> httpx.Client:
        if cls._sync_client is None:
            limits = httpx.Limits(max_keepalive_connections=30, max_connections=100)
            cls._sync_client = httpx.Client(limits=limits, timeout=30.0)
        return cls._sync_client

    @classmethod
    def get_async_client(cls) -> httpx.AsyncClient:
        if cls._async_client is None:
            limits = httpx.Limits(max_keepalive_connections=30, max_connections=100)
            cls._async_client = httpx.AsyncClient(limits=limits, timeout=30.0)
        return cls._async_client


# --- RATE LIMITER ORIGINALE ---
class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self.timestamps = deque()
        self.lock = threading.Lock()

    def wait_for_slot(self):
        now = time.time()
        with self.lock:
            cutoff = now - self.window
            while self.timestamps and self.timestamps[0] <= cutoff:
                self.timestamps.popleft()
            if len(self.timestamps) < self.max_requests:
                self.timestamps.append(time.time())
                return
            wait_duration = (self.timestamps[0] + self.window) - now

        if wait_duration > 0:
            time.sleep(wait_duration)

        with self.lock:
            self.timestamps.append(time.time())

songlink_rate_limiter = RateLimiter(9, 60.0)
zarz_rate_limiter = RateLimiter(5, 10.0)

@dataclass
class RetryConfig:
    max_attempts:   int   = 3
    base_delay_s:   float = 1.0
    max_delay_s:    float = 30.0
    backoff_factor: float = 2.0


# --- HTTP CLIENT SINCRONO (Ottimizzato con httpx) ---
class HttpClient:
    @property
    def _session(self):
        return self._client
    
    def __init__(
            self,
            provider:    str,
            timeout_s:   int            = 30,
            retry:       RetryConfig | None = None,
            headers:     dict[str, str] | None = None,
            rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._provider = provider
        self._timeout  = timeout_s
        self._retry    = retry or RetryConfig()
        self._client   = NetworkManager.get_sync_client()
        self._headers  = headers or {}
        self._limiter  = rate_limiter

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> dict:
        return self._parse_json(self.get(url, **kwargs))

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = self._headers.copy()
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
            
        delay = self._retry.base_delay_s
        last_err: Exception | None = None

        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                if self._limiter:
                    self._limiter.wait_for_slot()

                resp = self._client.request(method, url, headers=headers, timeout=self._timeout, **kwargs)
                self._raise_for_status(resp)
                return resp

            except RateLimitedError as exc:
                last_err = exc
                wait = getattr(exc, "retry_after", delay)
                time.sleep(wait)
            except httpx.RequestError as exc:
                last_err = NetworkError(self._provider, f"Errore di rete: {exc}")
                time.sleep(min(delay, self._retry.max_delay_s))
                delay *= self._retry.backoff_factor

        raise last_err

    def _raise_for_status(self, resp: httpx.Response) -> None:
        sc = resp.status_code
        if sc == 200: return
        if sc == 401: raise AuthError(self._provider, "Unauthorized (401)")
        if sc == 403: raise AuthError(self._provider, "Forbidden (403)")
        if sc == 404: raise TrackNotFoundError(self._provider, str(resp.url))
        if sc == 429: raise RateLimitedError(self._provider, int(resp.headers.get("Retry-After", 5)))
        if not resp.is_success: raise NetworkError(self._provider, f"HTTP {sc} from {resp.url}")

    def _parse_json(self, resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError:
            raise ParseError(self._provider, "Invalid JSON")

    def stream_to_file(self, url: str, dest_path: str, progress_cb: Any = None, chunk_size: int = 256 * 1024, extra_headers: dict | None = None):
        """Versione classica (stabile): Scaricamento sequenziale."""
        temp = dest_path + ".part"
        headers = extra_headers or {}
        try:
            if self._limiter: self._limiter.wait_for_slot()
            
            with self._client.stream("GET", url, headers=headers) as resp:
                self._raise_for_status(resp)
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
                
                with open(temp, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_cb: progress_cb(downloaded, total)
            os.replace(temp, dest_path)
        except Exception as exc:
            if os.path.exists(temp): os.remove(temp)
            raise NetworkError(self._provider, f"Stream failed: {exc}")

    def _classic_stream_to_file(self, url: str, dest_path: str, progress_cb: Any, chunk_size: int, headers: dict):
        """Il metodo di fallback sequenziale se il server non supporta il multi-parte."""
        temp = dest_path + ".part"
        with self._client.stream("GET", url, headers=headers) as resp:
            self._raise_for_status(resp)
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
            with open(temp, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb: progress_cb(downloaded, total)
        os.replace(temp, dest_path)
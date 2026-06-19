"""
HTTP client centralizzato con Connection Pooling globale e retry esponenziale.
Sostituisce 'requests' con 'httpx' per prestazioni nettamente superiori.

=== FASE 1 — Foundation HTTP (migrazione asyncio) ===
Aggiunte rispetto alla versione originale (vedi docs/asyncio-migration-plan.md):
  - AsyncRateLimiter: versione asyncio-safe di RateLimiter (asyncio.Lock + asyncio.sleep)
  - async_zarz_rate_limiter / async_songlink_rate_limiter: istanze globali parallele
    a quelle sync esistenti (non le sostituiscono)
  - NetworkManager.get_async_client_safe(): un AsyncClient per ogni event loop,
    per evitare il binding implicito di httpx.AsyncClient al loop in cui è stato creato
  - AsyncHttpClient: client HTTP asincrono per i provider, con get/post/get_json/stream_to_file

Tutto il codice sync esistente (RateLimiter, HttpClient, NetworkManager.get_sync_client,
NetworkManager.get_async_client) resta intatto per retrocompatibilità durante la
migrazione incrementale (Fasi 2-5).
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
import re

try:
    import aiofiles
except ImportError:  # aiofiles è un prerequisito di Fase 1, ma teniamo il fallback
    aiofiles = None


class _RedactUrlFilter(logging.Filter):
    _url_re = re.compile(r'https?://\S+')

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._url_re.sub('[endpoint]', record.getMessage())
        record.args = ()
        return True

logging.getLogger("httpx").addFilter(_RedactUrlFilter())

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

    _client_lock = threading.Lock()

    # ── Fase 1: registro multi-loop per i client asincroni ────────────────
    # Ogni event loop (es. quello dedicato della GUI vs quello di asyncio.run())
    # ottiene la propria istanza di httpx.AsyncClient, perché un AsyncClient
    # creato in un loop non può essere usato in modo sicuro da un loop diverso.
    _async_clients: dict[int, httpx.AsyncClient] = {}
    _async_clients_lock = threading.Lock()

    @classmethod
    def get_sync_client(cls) -> httpx.Client:
        if cls._sync_client is None:
            with cls._client_lock:
                if cls._sync_client is None:          # double-checked locking
                    limits = httpx.Limits(
                        max_keepalive_connections=30,
                        max_connections=100,
                    )
                    cls._sync_client = httpx.Client(limits=limits, timeout=30.0)
        return cls._sync_client

    @classmethod
    def get_async_client(cls) -> httpx.AsyncClient:
        """
        Versione legacy, mantenuta per retrocompatibilità con il codice già
        esistente (es. health_check.py, app.py) che gira tipicamente su un
        singolo event loop (asyncio.run()). NON thread/loop-safe se chiamata
        da più event loop diversi nello stesso processo.

        Per nuovo codice async (provider, AsyncHttpClient, ecc.) usare
        get_async_client_safe().
        """
        if cls._async_client is None:
            limits = httpx.Limits(max_keepalive_connections=30, max_connections=100)
            cls._async_client = httpx.AsyncClient(limits=limits, timeout=30.0)
        return cls._async_client

    @classmethod
    async def get_async_client_safe(cls) -> httpx.AsyncClient:
        """
        Versione async-safe (Fase 1): crea il client nel contesto dell'event
        loop corrente. Ogni event loop ottiene la propria istanza, così la
        GUI (loop dedicato in background thread) e gli script CLI
        (asyncio.run()) possono coesistere senza condividere un client
        legato a un loop diverso da quello attivo.
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
        """Chiude (e rimuove dal registro) il client async del loop corrente."""
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
        """Close any active httpx clients to release resources."""
        try:
            if cls._sync_client is not None:
                try:
                    cls._sync_client.close()
                except Exception:
                    pass
                cls._sync_client = None
            if cls._async_client is not None:
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # schedule close
                        loop.create_task(cls._async_client.aclose())
                    else:
                        loop.run_until_complete(cls._async_client.aclose())
                except Exception:
                    try:
                        cls._async_client.aclose()
                    except Exception:
                        pass
                cls._async_client = None
            # Best-effort cleanup dei client async multi-loop (Fase 1).
            # Se i loop corrispondenti sono già chiusi non possiamo più fare
            # await aclose(); ci limitiamo a svuotare il registro.
            with cls._async_clients_lock:
                cls._async_clients.clear()
        except Exception:
            pass


# --- RATE LIMITER ORIGINALE (sync, threading) ---
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


# --- RATE LIMITER ASINCRONO (Fase 1) ---
class AsyncRateLimiter:
    """
    Versione asyncio-safe di RateLimiter.
    Usa asyncio.Lock invece di threading.Lock e asyncio.sleep invece di time.sleep,
    così non blocca l'event loop mentre attende uno slot disponibile.

    L'asyncio.Lock è creato in modo lazy (alla prima wait_for_slot()) perché
    non può essere istanziato in modo sicuro fuori da un event loop attivo —
    vedi "I 3 punti assolutamente critici" nel piano di migrazione.
    """
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self.timestamps: deque = deque()
        self._lock: asyncio.Lock | None = None  # lazy init (il loop potrebbe non esistere ancora)

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


# Rate limiters async globali — paralleli a quelli sync esistenti.
# Non sostituiscono songlink_rate_limiter / zarz_rate_limiter: il codice sync
# (HttpClient, provider non ancora migrati) continua a usare quelli sopra.
async_zarz_rate_limiter     = AsyncRateLimiter(5, 10.0)
async_songlink_rate_limiter = AsyncRateLimiter(9, 60.0)


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
                last_err = NetworkError(self._provider, f"Error di rete: {exc}")
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

    def stream_to_file(self, url: str, dest_path: str, progress_cb: Any = None, chunk_size: int = 256 * 1024, extra_headers: dict | None = None, stop_event: Any = None):
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
                        # Check for external cancellation event
                        evt = stop_event or getattr(self, "_stop_event", None)
                        if evt is not None and getattr(evt, "is_set", lambda: False)():
                            if os.path.exists(temp):
                                os.remove(temp)
                            raise NetworkError(self._provider, "Stream cancelled by stop_event")
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_cb: progress_cb(downloaded, total)
            os.replace(temp, dest_path)
        except httpx.RequestError as exc:
            if os.path.exists(temp):
                os.remove(temp)
            raise NetworkError(self._provider, f"Stream failed: {exc}") from exc
        except OSError:
            if os.path.exists(temp):
                os.remove(temp)
            raise

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


# --- HTTP CLIENT ASINCRONO (Fase 1) ---
class AsyncHttpClient:
    """
    Client HTTP asincrono per i provider.
    Affianca HttpClient durante la transizione (Fasi 1-3): non lo sostituisce
    finché i singoli provider non vengono migrati a `async def download_track`.

    Usa NetworkManager.get_async_client_safe() così è sicuro da usare sia
    nel loop di asyncio.run() (CLI) sia nel loop dedicato della GUI.
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

    async def get_json(self, url: str, **kwargs: Any) -> dict:
        resp = await self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError:
            raise ParseError(self._provider, "Invalid JSON")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        if self._limiter:
            await self._limiter.wait_for_slot()
        client = await self._client()
        resp = await client.request(
            method, url,
            headers=headers,
            timeout=self._timeout,
            **kwargs,
        )
        self._raise_for_status(resp)
        return resp

    def _raise_for_status(self, resp: httpx.Response) -> None:
        sc = resp.status_code
        if sc == 200: return
        if sc == 401: raise AuthError(self._provider, "Unauthorized (401)")
        if sc == 403: raise AuthError(self._provider, "Forbidden (403)")
        if sc == 404: raise TrackNotFoundError(self._provider, str(resp.url))
        if sc == 429: raise RateLimitedError(self._provider, int(resp.headers.get("Retry-After", 5)))
        if not resp.is_success: raise NetworkError(self._provider, f"HTTP {sc} from {resp.url}")

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


# Ensure NetworkManager closes clients on process exit to avoid resource warnings in tests
import atexit as _atexit
_atexit.register(NetworkManager.close)
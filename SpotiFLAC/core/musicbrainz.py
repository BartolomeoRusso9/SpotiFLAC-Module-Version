"""
MusicBrainz API Client (Ported from Go implementation)
Gestisce rate-limiting globale, caching, deduplicazione in-flight e retry.
"""
from __future__ import annotations
import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)

# Costanti allineate al Go
_MB_API_BASE             = "https://musicbrainz.org/ws/2"
_MB_TIMEOUT              = 10
_MB_RETRIES              = 3
_MB_RETRY_WAIT           = 3.0
_MB_MIN_REQ_INTERVAL     = 1.1  # 1100ms
_MB_THROTTLE_COOLDOWN    = 5.0  # 5s su errore 503
_MB_STATUS_SKIP_WINDOW   = 300  # 5 minuti

_USER_AGENT = "SpotiFLAC/2.0 ( support@spotbye.qzz.io )"

# Stato globale (Thread-safe)
_mb_cache: dict[str, str] = {}
_mb_inflight: dict[str, threading.Event] = {}
_mb_inflight_results: dict[str, str | Exception] = {}
_mb_inflight_mu = threading.Lock()

_mb_throttle_mu = threading.Lock()
_mb_next_request: float = 0.0
_mb_blocked_till: float = 0.0

_mb_status_mu = threading.Lock()
_mb_last_checked_at: float = 0.0
_mb_last_checked_online: bool = True

def _wait_for_request_slot() -> None:
    """Accoda le richieste rispettando il limite di 1.1s (1100ms) tra l'una e l'altra."""
    global _mb_next_request

    with _mb_throttle_mu:
        ready_at = _mb_next_request
        if _mb_blocked_till > ready_at:
            ready_at = _mb_blocked_till

        now = time.time()
        if ready_at < now:
            ready_at = now

        _mb_next_request = ready_at + _MB_MIN_REQ_INTERVAL
        wait_duration = ready_at - now

    if wait_duration > 0:
        time.sleep(wait_duration)

def _note_throttle() -> None:
    """Applica un cooldown di 5 secondi se riceviamo un errore 503."""
    global _mb_blocked_till, _mb_next_request
    with _mb_throttle_mu:
        cooldown_until = time.time() + _MB_THROTTLE_COOLDOWN
        if cooldown_until > _mb_blocked_till:
            _mb_blocked_till = cooldown_until
        if _mb_next_request < _mb_blocked_till:
            _mb_next_request = _mb_blocked_till

def _query_recordings(query: str) -> dict:
    """Esegue la chiamata HTTP con retry logic."""
    url = f"{_MB_API_BASE}/recording?query={urllib.parse.quote(query)}&fmt=json&inc=releases+artist-credits+tags+media+release-groups+labels"
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json"
    }

    last_err = Exception("Empty response")

    for attempt in range(_MB_RETRIES):
        _wait_for_request_slot()

        try:
            resp = requests.get(url, headers=headers, timeout=_MB_TIMEOUT)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 503:
                _note_throttle()

            last_err = Exception(f"HTTP {resp.status_code}")

            # Non riprova sui 4xx (es. 400 Bad Request, 404 Not Found)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                break

        except requests.RequestException as e:
            last_err = e

        if attempt < _MB_RETRIES - 1:
            time.sleep(_MB_RETRY_WAIT)

    raise last_err

def fetch_genre_sync(isrc: str, use_single_genre: bool = True, separator: str = "; ") -> str:
    """
    Logica core tradotta dal Go. Usa l'in-flight deduplication per evitare
    che thread multipli chiamino l'API per lo stesso ISRC.
    """
    if not isrc:
        return ""

    cache_key = f"{isrc.strip().upper()}|{use_single_genre}|{separator}"

    # 1. Controlla la Cache
    if cache_key in _mb_cache:
        return _mb_cache[cache_key]

    # 2. In-flight Deduplication (Se un altro thread sta già cercando questo ISRC, aspetta lui)
    with _mb_inflight_mu:
        if cache_key in _mb_inflight:
            event = _mb_inflight[cache_key]
            _mb_inflight_mu.release()
            event.wait() # Aspetta che l'altro thread finisca
            res = _mb_inflight_results.get(cache_key, "")
            if isinstance(res, Exception):
                return ""
            return res

        # Siamo il primo thread a cercare questo ISRC
        event = threading.Event()
        _mb_inflight[cache_key] = event

    # 3. Fetch Effettivo
    final_genre = ""
    result_err = None
    try:
        data = _query_recordings(f"isrc:{isrc}")
        recordings = data.get("recordings", [])
        if not recordings:
            raise Exception("No recordings found")

        tags = recordings[0].get("tags", [])

        if use_single_genre:
            # Trova il tag con il "count" più alto
            best_tag = ""
            max_count = -1
            for t in tags:
                count = t.get("count", 0)
                if count > max_count:
                    max_count = count
                    best_tag = t.get("name", "")
            if best_tag:
                final_genre = best_tag.title()
        else:
            # Prendi i primi 5 tags
            genres = []
            for t in tags:
                genres.append(t.get("name", "").title())
            if genres:
                final_genre = separator.join(genres[:5])

        if final_genre:
            _mb_cache[cache_key] = final_genre

    except Exception as e:
        result_err = e
        logger.debug("[musicbrainz] Lookup failed for %s: %s", isrc, e)

    finally:
        # 4. Libera i thread in attesa
        with _mb_inflight_mu:
            _mb_inflight_results[cache_key] = result_err if result_err else final_genre
            del _mb_inflight[cache_key]
            event.set()

    return final_genre

# =====================================================================
# Wrapper Asincrono usato da Qobuz.py e Tidal.py
# =====================================================================
class AsyncGenreFetch:
    """
    Avvia la ricerca di MusicBrainz in background per non bloccare i download,
    sfruttando sotto il cofano il potente motore di cache e rate-limit.
    """
    # Usiamo un thread pool condiviso per tutte le ricerche
    _executor = ThreadPoolExecutor(max_workers=4)

    def __init__(self, isrc: str, use_single_genre: bool = True, separator: str = "; "):
        self.isrc = isrc
        self.future = self._executor.submit(fetch_genre_sync, isrc, use_single_genre, separator)

    def result(self) -> str:
        try:
            return self.future.result(timeout=15)
        except Exception:
            return ""
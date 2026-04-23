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

def fetch_mb_metadata(isrc: str) -> dict:
    """Recupera tutti i metadati disponibili da MusicBrainz via ISRC."""
    if not isrc:
        return {}

    # 1. Controllo cache (semplificato)
    cache_key = isrc.strip().upper()
    if cache_key in _mb_cache:
        return _mb_cache[cache_key]

    res = {
        "genre": "", "original_date": "", "bpm": "", "mbid_track": "",
        "mbid_album": "", "mbid_artist": "", "mbid_relgroup": "",
        "label": "", "barcode": "", "organization": "",
        "country": "", "script": "", "status": "",
        "media": "", "type": "", "artist_sort": ""
    }

    try:
        data = _query_recordings(f"isrc:{isrc}")
        recs = data.get("recordings", [])
        if not recs:
            return {}

        rec = recs[0]
        res["mbid_track"] = rec.get("id", "")
        res["original_date"] = rec.get("first-release-date", "")
        res["bpm"] = str(rec.get("bpm", "")) if rec.get("bpm") else ""

        credits = rec.get("artist-credit", [])
        if credits:
            artist_ids = []
            sort_names = []
            for c in credits:
                artist_obj = c.get("artist", {})
                a_id = artist_obj.get("id")
                a_sort = artist_obj.get("sort-name", "")
                phrase = c.get("joinphrase", "")

                if a_id:
                    artist_ids.append(a_id)
                if a_sort:
                    sort_names.append(a_sort + phrase)

            res["mbid_artist"] = "; ".join(artist_ids)
            res["artist_sort"] = "".join(sort_names)

        # Generi
        all_tags = rec.get("tags", [])
        for c in credits:
            all_tags.extend(c.get("artist", {}).get("tags", []))
        if all_tags:
            sorted_tags = sorted(all_tags, key=lambda x: x.get("count", 0), reverse=True)
            genres = []
            for t in sorted_tags:
                name = t.get("name", "").title()
                if name and name not in genres: genres.append(name)
            res["genre"] = "; ".join(genres[:5])

        # Release data
        releases = rec.get("releases", [])
        if releases:
            rel = releases[0]
            res["mbid_album"] = rel.get("id", "")
            res["mbid_relgroup"] = rel.get("release-group", {}).get("id", "")
            res["status"] = rel.get("status", "")
            res["type"] = rel.get("release-group", {}).get("primary-type", "")
            res["country"] = rel.get("country", "")
            res["script"] = rel.get("text-representation", {}).get("script", "")
            media = rel.get("media", [])
            if media: res["media"] = media[0].get("format", "")

            # Cerca il primo codice a barre valido in tutte le edizioni (releases)
            for r in releases:
                if r.get("barcode"):
                    res["barcode"] = r.get("barcode")
                    break

            # Cerca la prima etichetta valida in tutte le edizioni
            for r in releases:
                lbl_info = r.get("label-info", [])
                if lbl_info and lbl_info[0].get("label", {}).get("name"):
                    res["label"] = lbl_info[0].get("label", {}).get("name", "")
                    res["organization"] = res["label"]
                    break

        _mb_cache[cache_key] = res
    except Exception as e:
        logger.debug("[musicbrainz] lookup failed: %s", e)

    return res

# =====================================================================
# Wrapper Asincrono aggiornato per Metadati Completi
# =====================================================================
class AsyncMBFetch:
    """
    Avvia la ricerca di MusicBrainz in background.
    Ora restituisce un dizionario completo con tutti i metadati professionali.
    """
    # Thread pool condiviso
    _executor = ThreadPoolExecutor(max_workers=4)

    def __init__(self, isrc: str):
        self.isrc = isrc
        # Chiama la nuova funzione fetch_mb_metadata che restituisce il dict
        self.future = self._executor.submit(fetch_mb_metadata, isrc)

    def result(self) -> dict:
        """Ritorna il dizionario dei metadati. Se fallisce, ritorna un dict vuoto."""
        try:
            # Aspettiamo il risultato (max 15 secondi)
            return self.future.result(timeout=15)
        except Exception as e:
            logger.debug("[musicbrainz] Async fetch failed: %s", e)
            return {}
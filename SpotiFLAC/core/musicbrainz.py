"""
MusicBrainz API Client — versione originale sync + nuova variante async (Phase 2).
La variante async usa asyncio.Event per deduplicazione in-flight invece di threading.Event.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import atexit as _atexit

import httpx

from .http import NetworkManager
import threading as _threading

logger = logging.getLogger(__name__)

_MB_API_BASE             = "https://musicbrainz.org/ws/2"
_MB_TIMEOUT              = 6
_MB_RETRIES              = 2
_MB_RETRY_WAIT           = 1.5
_MB_MIN_REQ_INTERVAL     = 1.1
_MB_THROTTLE_COOLDOWN    = 5.0

_USER_AGENT = "SpotiFLAC/2.0 ( support@spotbye.qzz.io )"

_LOOKUP_FAILED = object()

_mb_cache: dict[str, object] = {}
_MB_CACHE_MAX = 2000
_mb_cache_order: list[str] = []
_mb_inflight: dict[str, threading.Event] = {}
_mb_inflight_mu = threading.Lock()

# --- Async in-flight state (Phase 2) ---
_mb_inflight_async: dict[str, asyncio.Event] = {}
_mb_inflight_async_lock: asyncio.Lock | None = None  # lazy init


def _get_async_inflight_lock() -> asyncio.Lock:
    global _mb_inflight_async_lock
    if _mb_inflight_async_lock is None:
        _mb_inflight_async_lock = asyncio.Lock()
    return _mb_inflight_async_lock


_mb_throttle_mu    = threading.Lock()
_mb_next_request:  float = 0.0
_mb_blocked_till:  float = 0.0

_mb_status_lock        = _threading.Lock()
_mb_last_checked_at:   float = 0.0
_mb_last_online:       bool  = True
_MB_STATUS_SKIP_WINDOW = 30.0


def set_mb_status(online: bool) -> None:
    global _mb_last_checked_at, _mb_last_online
    with _mb_status_lock:
        _mb_last_checked_at = time.time()
        _mb_last_online     = online


def should_skip_mb() -> bool:
    with _mb_status_lock:
        if _mb_last_checked_at == 0.0:
            return False
        if _mb_last_online:
            return False
        return (time.time() - _mb_last_checked_at) < _MB_STATUS_SKIP_WINDOW


def _wait_for_request_slot() -> None:
    global _mb_next_request

    with _mb_throttle_mu:
        ready_at = _mb_next_request
        if _mb_blocked_till > ready_at:
            ready_at = _mb_blocked_till

        now = time.time()
        if ready_at < now:
            ready_at = now

        _mb_next_request      = ready_at + _MB_MIN_REQ_INTERVAL
        wait_duration         = ready_at - now

    if wait_duration > 0:
        time.sleep(wait_duration)


async def _wait_for_request_slot_async() -> None:
    """Async-safe throttle per MusicBrainz (Phase 2)."""
    global _mb_next_request

    with _mb_throttle_mu:
        ready_at = _mb_next_request
        if _mb_blocked_till > ready_at:
            ready_at = _mb_blocked_till

        now = time.time()
        if ready_at < now:
            ready_at = now

        _mb_next_request  = ready_at + _MB_MIN_REQ_INTERVAL
        wait_duration     = ready_at - now

    if wait_duration > 0:
        await asyncio.sleep(wait_duration)


def _run_async_sync(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if loop.is_running():
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return loop.run_until_complete(coro)


def _note_throttle() -> None:
    global _mb_blocked_till, _mb_next_request
    with _mb_throttle_mu:
        cooldown_until = time.time() + _MB_THROTTLE_COOLDOWN
        if cooldown_until > _mb_blocked_till:
            _mb_blocked_till = cooldown_until
        if _mb_next_request < _mb_blocked_till:
            _mb_next_request = _mb_blocked_till


async def _query_recordings_async(query: str) -> dict:
    url = (
        f"{_MB_API_BASE}/recording"
        f"?query={urllib.parse.quote(query)}"
        f"&fmt=json&inc=releases+artist-credits+tags+media+release-groups+labels+label-info+isrcs"
    )
    headers  = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    last_err = Exception("Empty response")
    client   = await NetworkManager.get_async_client_safe()

    for attempt in range(_MB_RETRIES):
        await _wait_for_request_slot_async()
        try:
            resp = await client.get(url, headers=headers, timeout=_MB_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 503:
                _note_throttle()
            last_err = Exception(f"HTTP {resp.status_code}")
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                break
        except httpx.RequestError as e:
            last_err = e
        if attempt < _MB_RETRIES - 1:
            await asyncio.sleep(_MB_RETRY_WAIT)

    raise last_err


def _query_recordings(query: str) -> dict:
    return _run_async_sync(_query_recordings_async(query))


def _parse_mb_response(data: dict) -> dict:
    """Logica di parsing estratta per riutilizzo da sync e async."""
    parsed: dict = {
        "genre": "", "original_date": "", "bpm": "", "mbid_track": "",
        "mbid_album": "", "mbid_artist": "", "mbid_relgroup": "",
        "mbid_albumartist": "", "albumartist_sort": "", "catalognumber": "",
        "label": "", "barcode": "", "organization": "",
        "country": "", "script": "", "status": "",
        "media": "", "type": "", "artist_sort": ""
    }

    recs = data.get("recordings", [])
    if not recs:
        return parsed

    rec = recs[0]
    parsed["mbid_track"]    = rec.get("id", "")
    parsed["original_date"] = rec.get("first-release-date", "")
    parsed["bpm"]           = str(rec.get("bpm", "")) if rec.get("bpm") else ""

    credits = rec.get("artist-credit", [])
    if credits:
        artist_ids  = []
        sort_names  = []
        for c in credits:
            artist_obj = c.get("artist", {})
            a_id   = artist_obj.get("id")
            a_sort = artist_obj.get("sort-name", "")
            phrase = c.get("joinphrase", "")
            if a_id:
                artist_ids.append(a_id)
            if a_sort:
                sort_names.append(a_sort + phrase)
        parsed["mbid_artist"] = "; ".join(artist_ids)
        parsed["artist_sort"] = "".join(sort_names)

    all_tags = rec.get("tags", [])
    for c in credits:
        all_tags.extend(c.get("artist", {}).get("tags", []))
    if all_tags:
        sorted_tags = sorted(all_tags, key=lambda x: x.get("count", 0), reverse=True)
        genres = []
        for t in sorted_tags:
            name = t.get("name", "").title()
            if name and name not in genres:
                genres.append(name)
        parsed["genre"] = "; ".join(genres[:5])

    releases = rec.get("releases", [])
    if releases:
        def _release_score(r: dict) -> int:
            score = 0
            if r.get("barcode"):
                score += 2
            if r.get("label-info"):
                score += 2
            if r.get("country"):
                score += 1
            if r.get("status") == "Official":
                score += 1
            return score

        rel = max(releases, key=_release_score)
        parsed["mbid_album"]    = rel.get("id", "")
        parsed["mbid_relgroup"] = rel.get("release-group", {}).get("id", "")
        parsed["status"]        = rel.get("status", "")
        parsed["type"]          = rel.get("release-group", {}).get("primary-type", "")
        parsed["country"]       = rel.get("country", "")
        parsed["script"]        = rel.get("text-representation", {}).get("script", "")
        media                   = rel.get("media", [])
        if media:
            parsed["media"] = media[0].get("format", "")

        rel_credits = rel.get("artist-credit", [])
        if rel_credits:
            aa_ids        = []
            aa_sort_names = []
            for c in rel_credits:
                artist_obj = c.get("artist", {})
                a_id   = artist_obj.get("id")
                a_sort = artist_obj.get("sort-name", "")
                phrase = c.get("joinphrase", "")
                if a_id:
                    aa_ids.append(a_id)
                if a_sort:
                    aa_sort_names.append(a_sort + phrase)
            parsed["mbid_albumartist"] = "; ".join(aa_ids)
            parsed["albumartist_sort"] = "".join(aa_sort_names)

        for r in releases:
            if not parsed.get("barcode") and r.get("barcode"):
                parsed["barcode"] = r["barcode"]
            for li in r.get("label-info", []):
                lbl = li.get("label") or {}
                if not parsed.get("label") and lbl.get("name"):
                    parsed["label"]        = lbl["name"]
                    parsed["organization"] = lbl["name"]
                if not parsed.get("catalognumber") and li.get("catalog-number"):
                    parsed["catalognumber"] = li["catalog-number"]
            if parsed.get("barcode") and parsed.get("label") and parsed.get("catalognumber"):
                break

    return parsed


# ---------------------------------------------------------------------------
# Sync fetch_mb_metadata (invariato)
# ---------------------------------------------------------------------------

def fetch_mb_metadata(isrc: str) -> dict:
    if not isrc:
        return {}

    cache_key = isrc.strip().upper()
    cached    = _mb_cache.get(cache_key)
    if cached is not None:
        return {} if cached is _LOOKUP_FAILED else cached  # type: ignore

    if should_skip_mb():
        logger.debug("[musicbrainz] skipped (offline recently)")
        return {}

    with _mb_inflight_mu:
        if cache_key in _mb_inflight:
            event      = _mb_inflight[cache_key]
            is_leader  = False
        else:
            event      = threading.Event()
            _mb_inflight[cache_key] = event
            is_leader  = True

    if not is_leader:
        event.wait()
        result = _mb_cache.get(cache_key)
        return {} if (result is None or result is _LOOKUP_FAILED) else result  # type: ignore

    res: dict | object = _LOOKUP_FAILED
    try:
        data = _query_recordings(f"isrc:{isrc}")
        set_mb_status(True)
        res  = _parse_mb_response(data)
    except Exception as e:
        set_mb_status(False)
        logger.debug("[musicbrainz] lookup failed: %s", e)
        res = _LOOKUP_FAILED
    finally:
        _mb_cache[cache_key] = res
        try:
            _mb_cache_order.append(cache_key)
            if len(_mb_cache_order) > _MB_CACHE_MAX:
                old = _mb_cache_order.pop(0)
                _mb_cache.pop(old, None)
        except Exception:
            pass
        event.set()
        with _mb_inflight_mu:
            _mb_inflight.pop(cache_key, None)

    return {} if res is _LOOKUP_FAILED else res  # type: ignore


# ---------------------------------------------------------------------------
# Async fetch_mb_metadata_async (Phase 2 — nuovo)
# ---------------------------------------------------------------------------

async def fetch_mb_metadata_async(isrc: str) -> dict:
    """
    Versione async di fetch_mb_metadata.
    Usa asyncio.Event per deduplicazione in-flight invece di threading.Event.
    Stessa logica di caching della versione sync.
    """
    if not isrc:
        return {}

    cache_key = isrc.strip().upper()
    cached    = _mb_cache.get(cache_key)
    if cached is not None:
        return {} if cached is _LOOKUP_FAILED else cached  # type: ignore

    if should_skip_mb():
        logger.debug("[musicbrainz] async: skipped (offline recently)")
        return {}

    inflight_lock = _get_async_inflight_lock()

    async with inflight_lock:
        if cache_key in _mb_inflight_async:
            event = _mb_inflight_async[cache_key]
            await event.wait()
            result = _mb_cache.get(cache_key)
            return {} if (result is None or result is _LOOKUP_FAILED) else result  # type: ignore

        event = asyncio.Event()
        _mb_inflight_async[cache_key] = event

    res: dict | object = _LOOKUP_FAILED
    try:
        data = await _query_recordings_async(f"isrc:{isrc}")
        res = _parse_mb_response(data)
        set_mb_status(True)
    except Exception as e:
        set_mb_status(False)
        logger.debug("[musicbrainz] async lookup failed: %s", e)
        res = _LOOKUP_FAILED
    finally:
        _mb_cache[cache_key] = res
        try:
            _mb_cache_order.append(cache_key)
            if len(_mb_cache_order) > _MB_CACHE_MAX:
                old = _mb_cache_order.pop(0)
                _mb_cache.pop(old, None)
        except Exception:
            pass
        event.set()
        async with inflight_lock:
            _mb_inflight_async.pop(cache_key, None)

    return {} if res is _LOOKUP_FAILED else res  # type: ignore


def mb_result_to_tags(res: dict) -> dict[str, str]:
    """Converte il dizionario di risposta di MusicBrainz nei tag standard."""
    if not res:
        return {}

    mapping = {
        "mbid_track":       "MUSICBRAINZ_TRACKID",
        "mbid_album":       "MUSICBRAINZ_ALBUMID",
        "mbid_artist":      "MUSICBRAINZ_ARTISTID",
        "mbid_relgroup":    "MUSICBRAINZ_RELEASEGROUPID",
        "mbid_albumartist": "MUSICBRAINZ_ALBUMARTISTID",
        "barcode":          "BARCODE",
        "label":            "LABEL",
        "organization":     "ORGANIZATION",
        "country":          "RELEASECOUNTRY",
        "script":           "SCRIPT",
        "status":           "RELEASESTATUS",
        "media":            "MEDIA",
        "type":             "RELEASETYPE",
        "artist_sort":      "ARTISTSORT",
        "albumartist_sort": "ALBUMARTISTSORT",
        "catalognumber":    "CATALOGNUMBER",
        "bpm":              "BPM",
        "genre":            "GENRE",
    }

    tags = {}
    for mb_key, tag_name in mapping.items():
        val = res.get(mb_key)
        if val:
            tags[tag_name] = str(val)

    if res.get("original_date"):
        tags["ORIGINALDATE"] = res["original_date"]
        tags["ORIGINALYEAR"] = res["original_date"][:4]
    if res.get("catalognumber"):
        tags["CATALOGNUMBER"] = res["catalognumber"]

    return tags


# ---------------------------------------------------------------------------
# AsyncMBFetch — helper che wrappa ThreadPoolExecutor (backward compat)
# Per i provider già migrati ad async, usare direttamente fetch_mb_metadata_async.
# ---------------------------------------------------------------------------

class AsyncMBFetch:
    _executor: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=4)
    _executor_lock = threading.Lock()

    @classmethod
    def _shutdown(cls) -> None:
        with cls._executor_lock:
            if cls._executor is not None:
                cls._executor.shutdown(wait=False)
                cls._executor = None

    @classmethod
    def _get_executor(cls) -> ThreadPoolExecutor:
        with cls._executor_lock:
            if cls._executor is None:
                cls._executor = ThreadPoolExecutor(max_workers=4)
            return cls._executor

    def __init__(self, isrc: str):
        self.isrc = isrc
        try:
            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)
        except RuntimeError:
            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)

    def result(self, timeout: float | None = None) -> dict:
        try:
            return self.future.result(timeout=timeout)
        except Exception:
            return {}


_atexit.register(AsyncMBFetch._shutdown)

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
import threading as _threading

logger = logging.getLogger(__name__)

_MB_API_BASE             = "https://musicbrainz.org/ws/2"
_MB_TIMEOUT              = 6
_MB_RETRIES              = 2
_MB_RETRY_WAIT           = 1.5
_MB_MIN_REQ_INTERVAL     = 1.1
_MB_THROTTLE_COOLDOWN    = 5.0

_USER_AGENT = "SpotiFLAC/2.0 ( support@spotbye.qzz.io )"

_mb_cache: dict[str, dict] = {}
_mb_inflight: dict[str, threading.Event] = {}
# FIX #4: rimossa _mb_inflight_results — era definita ma mai letta né scritta (codice morto)
_mb_inflight_mu = threading.Lock()

_mb_throttle_mu = threading.Lock()
_mb_next_request: float = 0.0
_mb_blocked_till: float = 0.0

_mb_status_lock        = _threading.Lock()
_mb_last_checked_at:   float = 0.0
_mb_last_online:       bool  = True
_MB_STATUS_SKIP_WINDOW = 300.0


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

        _mb_next_request = ready_at + _MB_MIN_REQ_INTERVAL
        wait_duration = ready_at - now

    if wait_duration > 0:
        time.sleep(wait_duration)

def _note_throttle() -> None:
    global _mb_blocked_till, _mb_next_request
    with _mb_throttle_mu:
        cooldown_until = time.time() + _MB_THROTTLE_COOLDOWN
        if cooldown_until > _mb_blocked_till:
            _mb_blocked_till = cooldown_until
        if _mb_next_request < _mb_blocked_till:
            _mb_next_request = _mb_blocked_till

def _query_recordings(query: str) -> dict:
    url = f"{_MB_API_BASE}/recording?query={urllib.parse.quote(query)}&fmt=json&inc=releases+artist-credits+tags+media+release-groups+labels+label-info+isrcs"
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

            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                break

        except requests.RequestException as e:
            last_err = e

        if attempt < _MB_RETRIES - 1:
            time.sleep(_MB_RETRY_WAIT)

    raise last_err

def fetch_mb_metadata(isrc: str) -> dict:
    if not isrc:
        return {}

    cache_key = isrc.strip().upper()

    if cache_key in _mb_cache:
        return _mb_cache[cache_key]

    if should_skip_mb():
        logger.debug("[musicbrainz] skipped (offline recently)")
        return {}

    with _mb_inflight_mu:
        if cache_key in _mb_inflight:
            event = _mb_inflight[cache_key]
            is_leader = False
        else:
            event = threading.Event()
            _mb_inflight[cache_key] = event
            is_leader = True

    if not is_leader:
        event.wait()
        return _mb_cache.get(cache_key, {})

    res = {
        "genre": "", "original_date": "", "bpm": "", "mbid_track": "",
        "mbid_album": "", "mbid_artist": "", "mbid_relgroup": "",
        "mbid_albumartist": "", "albumartist_sort": "", "catalognumber": "",
        "label": "", "barcode": "", "organization": "",
        "country": "", "script": "", "status": "",
        "media": "", "type": "", "artist_sort": ""
    }

    try:
        data = _query_recordings(f"isrc:{isrc}")
        set_mb_status(True)
        recs = data.get("recordings", [])
        if recs:
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
                    if a_id: artist_ids.append(a_id)
                    if a_sort: sort_names.append(a_sort + phrase)
                res["mbid_artist"] = "; ".join(artist_ids)
                res["artist_sort"] = "".join(sort_names)

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

            releases = rec.get("releases", [])
            if releases:
                def _release_score(r: dict) -> int:
                    score = 0
                    if r.get("barcode"): score += 2
                    if r.get("label-info"): score += 2
                    if r.get("country"): score += 1
                    if r.get("status") == "Official": score += 1
                    return score

                rel = max(releases, key=_release_score)
                res["mbid_album"]    = rel.get("id", "")
                res["mbid_relgroup"] = rel.get("release-group", {}).get("id", "")
                res["status"]        = rel.get("status", "")
                res["type"]          = rel.get("release-group", {}).get("primary-type", "")
                res["country"]       = rel.get("country", "")
                res["script"]        = rel.get("text-representation", {}).get("script", "")
                media = rel.get("media", [])
                if media:
                    res["media"] = media[0].get("format", "")

                rel_credits = rel.get("artist-credit", [])
                if rel_credits:
                    aa_ids = []
                    aa_sort_names = []
                    for c in rel_credits:
                        artist_obj = c.get("artist", {})
                        a_id   = artist_obj.get("id")
                        a_sort = artist_obj.get("sort-name", "")
                        phrase = c.get("joinphrase", "")
                        if a_id:   aa_ids.append(a_id)
                        if a_sort: aa_sort_names.append(a_sort + phrase)
                    res["mbid_albumartist"] = "; ".join(aa_ids)
                    res["albumartist_sort"] = "".join(aa_sort_names)

                for r in releases:
                    if not res.get("barcode") and r.get("barcode"):
                        res["barcode"] = r["barcode"]
                    for li in r.get("label-info", []):
                        lbl = li.get("label") or {}
                        if not res.get("label") and lbl.get("name"):
                            res["label"]        = lbl["name"]
                            res["organization"] = lbl["name"]
                        if not res.get("catalognumber") and li.get("catalog-number"):
                            res["catalognumber"] = li["catalog-number"]
                    if res.get("barcode") and res.get("label") and res.get("catalognumber"):
                        break

        _mb_cache[cache_key] = res

    except Exception as e:
        set_mb_status(False)
        logger.debug("[musicbrainz] lookup failed: %s", e)
        return {}
    finally:
        event.set()
        with _mb_inflight_mu:
            _mb_inflight.pop(cache_key, None)

    return res


class AsyncMBFetch:
    """
    Avvia la ricerca di MusicBrainz in background.
    Restituisce un dizionario completo con tutti i metadati professionali.
    """
    _executor = ThreadPoolExecutor(max_workers=4)

    def __init__(self, isrc: str):
        self.isrc = isrc
        self.future = self._executor.submit(fetch_mb_metadata, isrc)

    def result(self) -> dict:
        try:
            return self.future.result(timeout=15)
        except Exception as e:
            logger.debug("[musicbrainz] Async fetch failed: %s", e)
            return {}
"""MusicBrainz API Client — original sync version + new async variant (Phase 2).
The async variant uses asyncio.Event for in-flight deduplication instead of threading.Event.
"""

from __future__ import annotations

import asyncio
import atexit as _atexit
import logging
import threading
import threading as _threading
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx

from .http import NetworkManager
from .loop_runner import run_sync
from .response_cache import get as get_cached_response
from .response_cache import put as put_cached_response

logger = logging.getLogger(__name__)

_MB_API_BASE = "https://musicbrainz.org/ws/2"
_MB_TIMEOUT = 6
_MB_RETRIES = 2
_MB_RETRY_WAIT = 1.5
_MB_MIN_REQ_INTERVAL = 1.1
_MB_THROTTLE_COOLDOWN = 5.0

_USER_AGENT = "SpotiFLAC/2.0 ( support@spotbye.qzz.io )"

_LOOKUP_FAILED = object()

#: Guards for the title/artist fallback (see _pick_fallback_recording()).
#: MusicBrainz knows plenty of recordings whose ISRC nobody ever linked —
#: whole national catalogues of them — and for those the isrc: search comes
#: back empty while a title/artist search finds the track at score 100. The
#: fallback exists for exactly that case, and these are what keep it from
#: writing another recording's MBIDs into the file: the search must be
#: confident, the title and a credited artist must line up, and the running
#: time must match to within a few seconds.
_MB_FALLBACK_MIN_SCORE = 95
_MB_FALLBACK_MAX_DELTA_MS = 3000
#: Plenty of MusicBrainz recordings carry no length at all — nobody entered
#: one — and refusing those outright left the fallback firing almost never.
#: They are accepted on a stricter reading instead: a top search score, a
#: title that is equal rather than merely compatible, and a credit list that
#: is *the same set of artists*, not just an overlapping one.
_MB_FALLBACK_STRICT_SCORE = 99

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


_mb_throttle_mu = threading.Lock()
_mb_next_request: float = 0.0
_mb_blocked_till: float = 0.0

_mb_status_lock = _threading.Lock()
_mb_last_checked_at: float = 0.0
_mb_last_online: bool = True
_MB_STATUS_SKIP_WINDOW = 30.0
_MB_RESPONSE_CACHE_TTL = 30 * 24 * 60 * 60


def set_mb_status(online: bool) -> None:
    global _mb_last_checked_at, _mb_last_online
    with _mb_status_lock:
        _mb_last_checked_at = time.time()
        _mb_last_online = online


def should_skip_mb() -> bool:
    return mb_skip_remaining() > 0.0


def mb_skip_remaining() -> float:
    """Seconds left of the post-failure pause, 0.0 when lookups are live.

    One failed request pauses *every* lookup for _MB_STATUS_SKIP_WINDOW, so
    a single timeout can leave a whole batch of tracks untagged. That is
    deliberate — MusicBrainz rate-limits hard and hammering it while it is
    refusing helps nobody — but it has to be visible in the log, which is
    what this is for.
    """
    with _mb_status_lock:
        if _mb_last_checked_at == 0.0 or _mb_last_online:
            return 0.0
        remaining = _MB_STATUS_SKIP_WINDOW - (time.time() - _mb_last_checked_at)
    return max(0.0, remaining)


def _wait_for_request_slot() -> None:
    global _mb_next_request

    with _mb_throttle_mu:
        ready_at = _mb_next_request
        ready_at = max(ready_at, _mb_blocked_till)

        now = time.time()
        ready_at = max(ready_at, now)

        _mb_next_request = ready_at + _MB_MIN_REQ_INTERVAL
        wait_duration = ready_at - now

    if wait_duration > 0:
        time.sleep(wait_duration)


async def _wait_for_request_slot_async() -> None:
    """Async-safe throttle per MusicBrainz (Phase 2)."""
    global _mb_next_request

    with _mb_throttle_mu:
        ready_at = _mb_next_request
        ready_at = max(ready_at, _mb_blocked_till)

        now = time.time()
        ready_at = max(ready_at, now)

        _mb_next_request = ready_at + _MB_MIN_REQ_INTERVAL
        wait_duration = ready_at - now

    if wait_duration > 0:
        await asyncio.sleep(wait_duration)


def _run_async_sync(coro):
    """Thin alias for loop_runner.run_sync() — see that function for why the
    old three-branch shim (which created a fresh loop in every branch) went
    away. Kept as a name so the many call sites below read unchanged.
    """
    return run_sync(coro)


def _note_throttle() -> None:
    global _mb_blocked_till, _mb_next_request
    with _mb_throttle_mu:
        cooldown_until = time.time() + _MB_THROTTLE_COOLDOWN
        _mb_blocked_till = max(_mb_blocked_till, cooldown_until)
        _mb_next_request = max(_mb_next_request, _mb_blocked_till)


async def _query_recordings_async(query: str) -> dict:
    url = (
        f"{_MB_API_BASE}/recording"
        f"?query={urllib.parse.quote(query)}"
        f"&fmt=json&inc=releases+artist-credits+tags+media+release-groups+labels+label-info+isrcs"
    )
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    last_err = Exception("Empty response")
    client = await NetworkManager.get_async_client_safe()

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


async def _query_recording_details_async(recording_id: str) -> dict:
    if not recording_id:
        return {}
    inc = "+".join(
        [
            "artist-credits",
            "releases",
            "release-groups",
            "media",
            "artist-rels",
            "work-rels",
            "url-rels",
            "genres",
            "aliases",
            "isrcs",
            "tags",
        ],
    )
    url = f"{_MB_API_BASE}/recording/{recording_id}"
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    last_err = Exception("Empty response")
    client = await NetworkManager.get_async_client_safe()

    for attempt in range(_MB_RETRIES):
        await _wait_for_request_slot_async()
        try:
            response = await client.get(
                url,
                params={"fmt": "json", "inc": inc},
                headers=headers,
                timeout=_MB_TIMEOUT,
            )
            if response.status_code == 200:
                return response.json()
            if response.status_code == 503:
                _note_throttle()
            last_err = Exception(f"HTTP {response.status_code}")
            if 400 <= response.status_code < 500 and response.status_code != 429:
                break
        except httpx.RequestError as e:
            last_err = e
        if attempt < _MB_RETRIES - 1:
            await asyncio.sleep(_MB_RETRY_WAIT)

    raise last_err


def _query_recording_details(recording_id: str) -> dict:
    return _run_async_sync(_query_recording_details_async(recording_id))


def _join_relation_artists(relations: list[dict], relation_type: str) -> str:
    names = []
    for relation in relations:
        if relation.get("type") != relation_type:
            continue
        artist = relation.get("artist") or {}
        name = artist.get("name")
        if name and name not in names:
            names.append(name)
    return "; ".join(names)


def _parse_mb_details(data: dict) -> dict:
    details: dict[str, str] = {}
    if not data:
        return details

    details["title"] = data.get("title", "")
    details["length"] = str(data.get("length", "")) if data.get("length") else ""
    details["disambiguation"] = data.get("disambiguation", "")
    details["video"] = str(bool(data.get("video"))).lower()
    relations = data.get("relations", [])
    for relation_type, tag_name in (
        ("composer", "composer"),
        ("lyricist", "lyricist"),
        ("producer", "producer"),
        ("performer", "performer"),
        ("remixer", "remixer"),
    ):
        details[tag_name] = _join_relation_artists(relations, relation_type)

    work_titles = []
    for relation in relations:
        work = relation.get("work") or {}
        if work.get("title") and work["title"] not in work_titles:
            work_titles.append(work["title"])
    details["work_title"] = "; ".join(work_titles)

    releases = data.get("releases", [])
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

        release = max(releases, key=_release_score)
        details["album"] = release.get("title", "")
        packaging = release.get("packaging", "")
        if packaging and packaging.lower() != "none":
            details["packaging"] = packaging
        details["quality"] = release.get("quality", "")
        details["release_date"] = release.get("date", "")
        details["secondary_types"] = "; ".join(
            release.get("release-group", {}).get("secondary-types", [])
        )
        media = release.get("media", [])
        if media:
            medium = media[0]
            details["disc_number"] = str(medium.get("position", ""))
            details["track_total"] = str(medium.get("track-count", ""))
            fallback_track = None
            for track in medium.get("tracks", []):
                rec_id = (
                    track.get("recording", {}).get("id")
                    if isinstance(track.get("recording"), dict)
                    else None
                )
                if rec_id == data.get("id"):
                    details["track_number"] = _track_number(track)
                    break
                if not fallback_track and track.get("title") == data.get("title"):
                    fallback_track = track
            if not details.get("track_number") and fallback_track:
                details["track_number"] = _track_number(fallback_track)
    return {key: value for key, value in details.items() if value}


def _track_number(track: dict) -> str:
    """A MusicBrainz track's number, as a number.

    Two fields could answer: `number`, the designation printed on the
    release, and `position`, the track's index on its medium. They agree on
    a CD and do not on a vinyl, where `number` is "A1", "B2" and so on — and
    a release group's vinyl pressing is a perfectly ordinary thing for
    _release_score() to pick. "B2" then travelled all the way into the
    tagger as TRACKNUMBER.

    Returns "" when neither field is usable, so that
    fetch_mb_metadata()'s filter drops the key and the provider's own track
    number is left in place rather than overwritten with something worse.
    """
    number = str(track.get("number", "") or "").strip()
    if number.isdigit():
        return number
    position = track.get("position")
    if isinstance(position, int) and position > 0:
        return str(position)
    return ""


def _query_recordings(query: str) -> dict:
    return _run_async_sync(_query_recordings_async(query))


def _parse_mb_response(data: dict) -> dict:
    """Logica di parsing estratta per riutilizzo da sync e async."""
    parsed: dict = {
        "genre": "",
        "original_date": "",
        "bpm": "",
        "mbid_track": "",
        "mbid_album": "",
        "mbid_artist": "",
        "mbid_relgroup": "",
        "mbid_albumartist": "",
        "albumartist_sort": "",
        "catalognumber": "",
        "label": "",
        "barcode": "",
        "organization": "",
        "country": "",
        "script": "",
        "status": "",
        "media": "",
        "type": "",
        "artist_sort": "",
    }

    recs = data.get("recordings", [])
    if not recs:
        return parsed

    rec = recs[0]
    parsed["mbid_track"] = rec.get("id", "")
    parsed["original_date"] = rec.get("first-release-date", "")
    parsed["bpm"] = str(rec.get("bpm", "")) if rec.get("bpm") else ""

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
        parsed["mbid_album"] = rel.get("id", "")
        parsed["mbid_relgroup"] = rel.get("release-group", {}).get("id", "")
        parsed["status"] = rel.get("status", "")
        parsed["type"] = rel.get("release-group", {}).get("primary-type", "")
        parsed["country"] = rel.get("country", "")
        parsed["script"] = rel.get("text-representation", {}).get("script", "")
        media = rel.get("media", [])
        if media:
            parsed["media"] = media[0].get("format", "")

        rel_credits = rel.get("artist-credit", [])
        if rel_credits:
            aa_ids = []
            aa_sort_names = []
            for c in rel_credits:
                artist_obj = c.get("artist", {})
                a_id = artist_obj.get("id")
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
                    parsed["label"] = lbl["name"]
                    parsed["organization"] = lbl["name"]
                if not parsed.get("catalognumber") and li.get("catalog-number"):
                    parsed["catalognumber"] = li["catalog-number"]
            if (
                parsed.get("barcode")
                and parsed.get("label")
                and parsed.get("catalognumber")
            ):
                break

    return parsed


# ---------------------------------------------------------------------------
# Title/artist fallback — used only when the ISRC is unknown to MusicBrainz
# ---------------------------------------------------------------------------


def _norm_text(value: str) -> str:
    """Lowercased, accent-free, punctuation-free form for comparison.

    Accents are folded rather than kept because the same artist is written
    both ways in the wild — "Guè" here, "Gué Pequeno" on MusicBrainz — and a
    comparison that treats those as different names throws away the match
    this fallback exists to find.
    """
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = "".join(c if c.isalnum() else " " for c in stripped.lower())
    return " ".join(cleaned.split())


def _norm_title(value: str) -> str:
    """`_norm_text` minus the decorations a title picks up on the way here.

    Spotify hands us "Cyborg (feat. Geolier)", "L'ultima volta - feat.
    Massimo Pericolo", "MI Fist (2004 Remaster)"; MusicBrainz stores the bare
    recording title. Cutting the bracketed and dashed tails is what lets the
    two meet.
    """
    text = str(value or "")
    for opener, closer in (("(", ")"), ("[", "]")):
        while opener in text and closer in text[text.index(opener) :]:
            start = text.index(opener)
            end = text.index(closer, start)
            inner = text[start + 1 : end].lower()
            if any(k in inner for k in ("feat", "with", "remaster", "version")):
                text = text[:start] + text[end + 1 :]
            else:
                break
    for separator in (" - ", " – "):
        head, sep, tail = text.partition(separator)
        if sep and any(
            k in tail.lower() for k in ("feat", "remaster", "version", "edit", "mix")
        ):
            text = head
    return _norm_text(text)


def _title_matches(ours: str, theirs: str) -> bool:
    """True when the two name the same recording.

    Prefixes only count at a word boundary: "Phra (Outro)" may meet "Phra",
    but "Love" must not meet "Lovers".
    """
    a_core, b_core = _norm_title(ours), _norm_title(theirs)
    if not a_core or not b_core:
        return False
    return (
        _norm_text(ours) == _norm_text(theirs)
        or a_core == b_core
        or a_core.startswith(b_core + " ")
        or b_core.startswith(a_core + " ")
    )


def _artist_matches(ours: str, theirs: str) -> bool:
    """True when one credit is contained in the other.

    Subset in either direction, because both happen: our "Guè" against
    MusicBrainz's "Gué Pequeno" (the same person, renamed), and our "Noyz
    Narcos, Chicoria" against a MusicBrainz credit for either of the two.
    "Gemello" against "Guè" shares nothing and is refused.
    """
    a = set(_norm_text(ours).split())
    b = set(_norm_text(theirs).split())
    if not a or not b:
        return False
    return a <= b or b <= a


def _credited_names(recording: dict) -> list[str]:
    names = []
    for credit in recording.get("artist-credit") or []:
        artist = credit.get("artist") or {}
        name = artist.get("name") or credit.get("name") or ""
        if name:
            names.append(name)
    return names


#: How a joined credit ("Noyz Narcos, Chicoria", "Drake & Future") is cut
#: back to the name to search MusicBrainz for.
_CREDIT_SEPARATORS = (",", "&", " feat.", " feat ", " ft.", " ft ", " with ", " x ")


def _split_credits(value: str) -> list[str]:
    """A joined credit as the list of names it holds."""
    text = str(value or "")
    parts = [text]
    for separator in _CREDIT_SEPARATORS:
        split_parts: list[str] = []
        for part in parts:
            lowered = part.lower()
            start = 0
            while True:
                found = lowered.find(separator, start)
                if found < 0:
                    split_parts.append(part[start:])
                    break
                split_parts.append(part[start:found])
                start = found + len(separator)
        parts = split_parts
    return [p.strip() for p in parts if p.strip()]


def _primary_artist(value: str) -> str:
    """The first name in a joined credit.

    MusicBrainz stores one artist per credit and matches the query against
    each; searching for the whole joined string ("noyz narcos chicoria")
    finds nothing at all, even when the recording is right there under the
    first of those names.
    """
    names = _split_credits(value)
    return names[0] if names else str(value or "").strip()


def _artist_credits_equivalent(ours: str, theirs: list[str]) -> bool:
    """True when both sides credit exactly the same set of artists."""
    ours_set = {_norm_text(name) for name in _split_credits(ours)} - {""}
    theirs_set = {_norm_text(name) for name in theirs} - {""}
    return bool(ours_set) and ours_set == theirs_set


def _fallback_query(title: str, artist: str) -> str:
    """A Lucene query for the bare title and the primary artist."""
    safe_title = _norm_title(title) or _norm_text(title)
    safe_artist = _norm_text(_primary_artist(artist))
    return f'recording:"{safe_title}" AND artist:"{safe_artist}"'


def _pick_fallback_recording(
    recordings: list[dict],
    *,
    title: str,
    artist: str,
    duration_ms: int,
) -> dict | None:
    """The first candidate that clears every guard, or None.

    MusicBrainz returns them best-first. With a running time on both sides a
    candidate needs a score of at least _MB_FALLBACK_MIN_SCORE, a title that
    matches, a credited artist that matches, and a duration within
    _MB_FALLBACK_MAX_DELTA_MS — that last one being what keeps a live
    version, a re-recording or a cover from being accepted as this exact
    recording, which is what the MusicBrainz IDs written afterwards claim.

    Without a running time (MusicBrainz often has none) the duration check
    is replaced rather than waived: near-perfect score, an equal title, and
    the same set of credited artists. See _MB_FALLBACK_STRICT_SCORE.
    """
    for recording in recordings:
        try:
            score = int(recording.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score < _MB_FALLBACK_MIN_SCORE:
            continue

        mb_title = recording.get("title") or ""
        names = _credited_names(recording)
        try:
            length = int(recording.get("length") or 0)
        except (TypeError, ValueError):
            length = 0

        if length and duration_ms:
            if not _title_matches(title, mb_title):
                continue
            if not any(_artist_matches(artist, name) for name in names):
                continue
            if abs(length - int(duration_ms)) > _MB_FALLBACK_MAX_DELTA_MS:
                continue
            return recording

        # No running time to compare on either side — see
        # _MB_FALLBACK_STRICT_SCORE.
        if score < _MB_FALLBACK_STRICT_SCORE:
            continue
        if _norm_title(title) != _norm_title(mb_title) and _norm_text(
            title
        ) != _norm_text(mb_title):
            continue
        if not _artist_credits_equivalent(artist, names):
            continue
        return recording
    return None


def _log_fallback_hit(isrc: str, recording: dict) -> None:
    logger.info(
        "[musicbrainz] ISRC %s is not linked on MusicBrainz; matched "
        "'%s — %s' by title, artist and duration instead (%s)",
        isrc,
        recording.get("title", "?"),
        ", ".join(_credited_names(recording)) or "?",
        recording.get("id", "?"),
    )


def _log_no_match(isrc: str, title: str, artist: str) -> None:
    where = f" ({title} — {artist})" if title and artist else ""
    logger.info(
        "[musicbrainz] no match for ISRC %s%s — the recording is not on "
        "MusicBrainz, or its ISRC is not linked there; no MusicBrainz tags "
        "for this track",
        isrc,
        where,
    )


def _log_paused(isrc: str) -> None:
    logger.info(
        "[musicbrainz] lookups paused for another %.0fs after a failed "
        "request — no MusicBrainz tags for ISRC %s",
        mb_skip_remaining(),
        isrc,
    )


def _log_failed(isrc: str, exc: Exception) -> None:
    logger.warning(
        "[musicbrainz] lookup failed for ISRC %s (%s) — no MusicBrainz tags "
        "for this track",
        isrc,
        exc,
    )


# ---------------------------------------------------------------------------
# Sync fetch_mb_metadata (invariato)
# ---------------------------------------------------------------------------


def fetch_mb_metadata(
    isrc: str,
    *,
    title: str = "",
    artist: str = "",
    duration_ms: int = 0,
) -> dict:
    """MusicBrainz tags for `isrc`, `{}` when there is no confident match.

    `title`/`artist`/`duration_ms` are optional and only used when the ISRC
    itself is unknown to MusicBrainz — see _pick_fallback_recording() for
    what a match has to satisfy before it is accepted. Callers that pass
    nothing behave exactly as before: ISRC or nothing.
    """
    if not isrc:
        return {}

    cache_key = isrc.strip().upper()
    cached = _mb_cache.get(cache_key)
    if cached is not None:
        return {} if cached is _LOOKUP_FAILED else cached  # type: ignore
    persisted = get_cached_response("musicbrainz", cache_key, _MB_RESPONSE_CACHE_TTL)
    if isinstance(persisted, dict):
        _mb_cache[cache_key] = persisted
        return persisted

    if should_skip_mb():
        _log_paused(cache_key)
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
        result = _mb_cache.get(cache_key)
        return {} if (result is None or result is _LOOKUP_FAILED) else result  # type: ignore

    res: dict | object = _LOOKUP_FAILED
    try:
        data = _query_recordings(f"isrc:{isrc}")
        set_mb_status(True)
        res = _parse_mb_response(data)
        if not any(res.values()) and title and artist:
            candidates = _query_recordings(_fallback_query(title, artist))
            match = _pick_fallback_recording(
                candidates.get("recordings", []),
                title=title,
                artist=artist,
                duration_ms=duration_ms,
            )
            if match is not None:
                res = _parse_mb_response({"recordings": [match]})
                _log_fallback_hit(cache_key, match)
        if not any(res.values()):
            _log_no_match(cache_key, title, artist)
        try:
            res.update(
                _parse_mb_details(_query_recording_details(res.get("mbid_track", "")))
            )
        except (RuntimeError, httpx.RequestError) as detail_err:
            logger.debug(
                "[musicbrainz] detail query failed, keeping search result: %s",
                detail_err,
            )
        if res and any(res.values()):
            put_cached_response("musicbrainz", cache_key, res)
    except Exception as e:
        set_mb_status(False)
        _log_failed(cache_key, e)
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
# Async fetch_mb_metadata_async (Phase 2 — new)
# ---------------------------------------------------------------------------


async def fetch_mb_metadata_async(
    isrc: str,
    *,
    title: str = "",
    artist: str = "",
    duration_ms: int = 0,
) -> dict:
    """Async version of fetch_mb_metadata.
    Uses asyncio.Event for in-flight deduplication instead of threading.Event.
    Same caching logic — and the same optional title/artist fallback — as the
    sync version.
    """
    if not isrc:
        return {}

    cache_key = isrc.strip().upper()
    cached = _mb_cache.get(cache_key)
    if cached is not None:
        return {} if cached is _LOOKUP_FAILED else cached  # type: ignore
    persisted = get_cached_response("musicbrainz", cache_key, _MB_RESPONSE_CACHE_TTL)
    if isinstance(persisted, dict):
        _mb_cache[cache_key] = persisted
        return persisted

    if should_skip_mb():
        _log_paused(cache_key)
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
        if not any(res.values()) and title and artist:
            candidates = await _query_recordings_async(_fallback_query(title, artist))
            match = _pick_fallback_recording(
                candidates.get("recordings", []),
                title=title,
                artist=artist,
                duration_ms=duration_ms,
            )
            if match is not None:
                res = _parse_mb_response({"recordings": [match]})
                _log_fallback_hit(cache_key, match)
        if not any(res.values()):
            _log_no_match(cache_key, title, artist)
        try:
            details = await _query_recording_details_async(res.get("mbid_track", ""))
            res.update(_parse_mb_details(details))
        except (RuntimeError, httpx.RequestError) as detail_err:
            logger.debug(
                "[musicbrainz] async detail query failed, keeping search result: %s",
                detail_err,
            )
        if res and any(res.values()):
            put_cached_response("musicbrainz", cache_key, res)
        set_mb_status(True)
    except Exception as e:
        set_mb_status(False)
        _log_failed(cache_key, e)
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
    """Convert the MusicBrainz response dictionary into standard tags."""
    if not res:
        return {}

    mapping = {
        "mbid_track": "MUSICBRAINZ_TRACKID",
        "mbid_album": "MUSICBRAINZ_ALBUMID",
        "mbid_artist": "MUSICBRAINZ_ARTISTID",
        "mbid_relgroup": "MUSICBRAINZ_RELEASEGROUPID",
        "mbid_albumartist": "MUSICBRAINZ_ALBUMARTISTID",
        "barcode": "BARCODE",
        "label": "LABEL",
        "organization": "ORGANIZATION",
        "country": "RELEASECOUNTRY",
        "script": "SCRIPT",
        "status": "RELEASESTATUS",
        "media": "MEDIA",
        "type": "RELEASETYPE",
        "artist_sort": "ARTISTSORT",
        "albumartist_sort": "ALBUMARTISTSORT",
        "catalognumber": "CATALOGNUMBER",
        "bpm": "BPM",
        "genre": "GENRE",
        "composer": "COMPOSER",
        "lyricist": "LYRICIST",
        "producer": "PRODUCER",
        "performer": "PERFORMER",
        "remixer": "REMIXER",
        "work_title": "WORKTITLE",
        "album": "ALBUM",
        "release_date": "RELEASEDATE",
        "packaging": "PACKAGING",
        "quality": "RELEASEQUALITY",
        "secondary_types": "RELEASESECONDARYTYPE",
        "disambiguation": "DISAMBIGUATION",
        "length": "DURATIONMS",
        "track_number": "TRACKNUMBER",
        "disc_number": "DISCNUMBER",
        "track_total": "TRACKTOTAL",
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
# AsyncMBFetch — helper wrapping ThreadPoolExecutor (backward compat)
# For providers already migrated to async, use fetch_mb_metadata_async directly.
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

    def __init__(self, isrc: str) -> None:
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

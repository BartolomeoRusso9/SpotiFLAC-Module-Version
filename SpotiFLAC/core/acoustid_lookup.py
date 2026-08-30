"""SpotiFLAC/core/acoustid_lookup.py — identify a local file by its sound.

Everything else in the local tagger reasons about a file from its *tags*,
which is circular for the one job it exists to do: a file with wrong tags is
searched for under the wrong name. An acoustic fingerprint is the only
signal that escapes that, because it describes the audio rather than what
someone wrote about it.

This is deliberately narrow. It asks AcoustID one question — "which
recording is this?" — and answers with an ISRC, because an ISRC is what the
rest of SpotiFLAC already knows how to turn into a Spotify track
(link_resolver.spotify_url_for_isrc_async, local_matcher's `isrc=`
argument). Nothing else here needs to change to consume it.

Why it is a fallback and not the default
----------------------------------------
AcoustID's free tier allows 3 requests/second *per application key*, and
SpotiFLAC ships one key for everybody (see get_acoustid_config()). That
budget is shared across every user in the world, so it is spent only on the
files the cheap path could not resolve — typically a handful per folder,
which is seconds of traffic. Fingerprinting a whole library on every scan
would exhaust it for everyone at once. A user who needs more can set their
own key in Settings, which takes precedence.

It also needs `fpcalc` (Chromaprint) on PATH, exactly like the duplicate
finder; without it is_available() is False and callers skip the step rather
than failing.
"""

from __future__ import annotations

import logging

from .audio_fingerprint import AudioFingerprintError, compute_fingerprint_async
from .audio_fingerprint import is_available as fingerprinting_available
from .http import AsyncRateLimiter, NetworkManager
from .isrc_utils import normalize_isrc

logger = logging.getLogger(__name__)

#: AcoustID's free tier is 3 requests/second. Enforced here rather than
#: relied upon: a 429 is a failed identification, and this costs nothing
#: when the caller is only ever sending a handful of requests anyway.
_rate_limiter = AsyncRateLimiter(3, 1.0)

#: Below this AcoustID's own match score, the result is not trusted. Its
#: score is 0…1 and a correct identification is normally very close to 1;
#: anything materially lower means the fingerprint matched something only
#: loosely, which is exactly the case where guessing is worse than saying
#: nothing.
MIN_SCORE = 0.8


def _configured_key(settings_key: str = "") -> tuple[str, str]:
    """(client key, lookup URL). A key from the user's settings wins over
    the shared one, so somebody running this heavily is not competing with
    every other user for the same 3 requests/second.
    """
    from . import get_acoustid_config

    cfg = get_acoustid_config()
    client = (settings_key or "").strip() or str(cfg.get("client") or "").strip()
    url = str(cfg.get("lookup") or "").strip() or "https://api.acoustid.org/v2/lookup"
    return client, url


def is_available(settings_key: str = "") -> bool:
    """Whether identification can run: fpcalc present and a key configured."""
    if not fingerprinting_available():
        return False
    client, _ = _configured_key(settings_key)
    return bool(client)


def _best_isrc(payload: dict) -> tuple[str, float, str]:
    """Pulls the highest-scoring ISRC out of a /v2/lookup response.

    The shape is results[].recordings[].isrcs[], and every level is
    optional — a fingerprint can match a recording AcoustID knows about but
    that carries no ISRC, which is a miss for our purposes, not an error.
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return "", 0.0, ""

    best_isrc, best_score, best_id = "", 0.0, ""
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        try:
            score = float(result.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        if score <= best_score:
            continue
        for recording in result.get("recordings") or []:
            if not isinstance(recording, dict):
                continue
            for raw in recording.get("isrcs") or []:
                isrc = normalize_isrc(str(raw))
                if isrc:
                    best_isrc, best_score = isrc, score
                    # AcoustID's own id for the recording. Worth writing into
                    # the file: it makes every future identification free,
                    # and it is the tag MusicBrainz Picard reads too.
                    best_id = str(result.get("id") or "")
                    break
            if best_isrc and best_score == score:
                break
    return best_isrc, best_score, best_id


async def identify_isrc_async(
    file_path,
    *,
    settings_key: str = "",
    timeout_s: int = 20,
) -> tuple[str, str]:
    """(ISRC, AcoustID id) for whatever recording `file_path` contains.

    Both are "" when the file could not be identified.

    Never raises: an unavailable fpcalc, an unconfigured key, a network
    failure, a rate-limited response or a fingerprint AcoustID has never
    seen all mean the same thing to the caller — no answer — and none of
    them should abort a folder scan.
    """
    client, url = _configured_key(settings_key)
    if not client:
        logger.debug("[acoustid] no application key configured; skipping lookup")
        return "", ""

    try:
        fingerprint = await compute_fingerprint_async(file_path)
    except AudioFingerprintError as exc:
        logger.debug("[acoustid] could not fingerprint %s: %s", file_path, exc)
        return "", ""

    if not fingerprint.compressed or not fingerprint.duration_s:
        logger.debug("[acoustid] empty fingerprint for %s", file_path)
        return "", ""

    await _rate_limiter.wait_for_slot()

    # httpx directly rather than AsyncHttpClient, for two reasons specific to
    # this endpoint. AcoustID reports its own errors as HTTP 400 with a JSON
    # body naming the problem ("invalid API key", "invalid fingerprint"), and
    # AsyncHttpClient raises on any non-200 — so the body, which is the only
    # useful diagnostic, would be thrown away. Worse, it classifies a 400 as
    # NetworkError, which is in its retry list: a permanently invalid request
    # would be re-sent several times, spending a rate limit that is shared
    # with every other user of the application key.
    try:
        http = await NetworkManager.get_async_client_safe()
        # POST rather than GET: a compressed fingerprint is several KB and
        # would not survive a URL, and AcoustID documents form-encoded POST
        # for exactly this reason.
        response = await http.post(
            url,
            data={
                "client": client,
                "duration": str(int(round(fingerprint.duration_s))),
                "fingerprint": fingerprint.compressed,
                # "isrcs" alone, not "recordings+isrcs". Combining the two
                # is what the documented syntax suggests, and it silently
                # returns results with zero recordings attached — no error,
                # just nothing to read. "isrcs" on its own returns the
                # recordings *and* their ISRCs. Verified against the live
                # endpoint; do not "fix" this back.
                "meta": "isrcs",
            },
            timeout=timeout_s,
        )
        payload = response.json()
    except Exception as exc:
        logger.debug("[acoustid] lookup failed for %s: %s", file_path, exc)
        return "", ""

    if not isinstance(payload, dict) or payload.get("status") != "ok":
        message = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            message = (
                (error or {}).get("message", "") if isinstance(error, dict) else ""
            )
        logger.debug(
            "[acoustid] lookup rejected for %s: %s",
            file_path,
            message or f"HTTP {getattr(response, 'status_code', '?')}",
        )
        return "", ""

    isrc, score, acoustid_id = _best_isrc(payload)
    if not isrc:
        logger.debug("[acoustid] no ISRC for %s", file_path)
        return "", ""
    if score < MIN_SCORE:
        logger.debug(
            "[acoustid] discarding %s for %s: score %.2f below %.2f",
            isrc,
            file_path,
            score,
            MIN_SCORE,
        )
        return "", ""

    logger.debug("[acoustid] %s identified as %s (score %.2f)", file_path, isrc, score)
    return isrc, acoustid_id

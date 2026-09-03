"""SpotiFLAC/core/recording_guard.py — catching a provider that resolved the
wrong *recording* of the right song.

Every provider is asked for one specific recording, identified by the ISRC
that came from Spotify. Some of them answer with a different one and say so:
a provider's search resolves a candidate, and it then writes that candidate's
ISRC (and label, copyright, release date, cover) back onto the TrackMetadata
it was handed. That write is the signal this module reads.

Three failures observed in one playlist run, all of them silent:

  * "Like Him (feat. Lola Young)" (USQX92405794) came back as QZYD92602058 —
    "Like Him … (Melody Karaoke Version)" by ZZang KARAOKE. Right length,
    right song title, no voice.
  * "Hours In Silence" (USUG12208604) came back as USUG12208620, and
    "Spin Bout U" (USUG12208603) as USUG12208619 — the clean edits, with the
    profanity muted.

Each produced a file carrying the requested track's tags, so nothing
downstream could tell it was wrong. A drifted ISRC is not proof of a wrong
recording on its own, though — a provider that serves a remaster answers
"Is It a Crime" (GBBBM8500014) with GBARL1100322, the same performance under
a later release — so the drifted ISRC is looked up and *judged* rather than
rejected on sight. The lookup is Deezer's public ISRC endpoint, which is
already this project's second opinion on a recording elsewhere
(metadata_enrichment._DeezerMeta).

Only a positive disagreement rejects. A lookup that fails, times out or
returns nothing leaves the download alone: an unreachable Deezer must not
turn into a failed download.
"""

from __future__ import annotations

import logging
from typing import Any

from .isrc_utils import normalize_isrc
from .response_cache import get as get_cached_response
from .response_cache import put as put_cached_response
from .text_match import (
    DURATION_TOLERANCE_MS,
    artists_match,
    titles_match,
    variant_conflict,
)

logger = logging.getLogger(__name__)

_DEEZER_ISRC = "https://api.deezer.com/track/isrc:{isrc}"
_TIMEOUT_S = 5.0
_CACHE_NS = "recording-guard"
_CACHE_TTL = 7 * 24 * 60 * 60

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


async def _lookup_isrc_async(isrc: str) -> dict[str, Any] | None:
    """What Deezer knows about `isrc`, or None if it could not say.

    Cached on disk: the answer for an ISRC does not change, and a run that
    hits the same drifted ISRC on several tracks of one album should ask
    once.
    """
    cached = get_cached_response(_CACHE_NS, isrc, _CACHE_TTL)
    if isinstance(cached, dict):
        return cached or None

    try:
        from .http import NetworkManager

        client = await NetworkManager.get_async_client_safe()
        resp = await client.get(
            _DEEZER_ISRC.format(isrc=isrc),
            timeout=_TIMEOUT_S,
            headers={"User-Agent": _UA},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as exc:
        logger.debug("[recording-guard] lookup of %s failed: %s", isrc, exc)
        return None

    if not isinstance(data, dict) or "error" in data or not data.get("id"):
        # A negative answer is cached too — an ISRC Deezer does not carry
        # will still not be there on the next track of the same album.
        put_cached_response(_CACHE_NS, isrc, {})
        return None

    found = {
        "title": str(data.get("title") or ""),
        "artist": str((data.get("artist") or {}).get("name") or ""),
        "duration_ms": int(data.get("duration") or 0) * 1000,
        "explicit": bool(data.get("explicit_lyrics")),
    }
    put_cached_response(_CACHE_NS, isrc, found)
    return found


def judge_resolved_recording(
    *,
    expected_title: str,
    expected_artist: str,
    expected_duration_ms: int,
    expected_explicit: bool,
    found: dict[str, Any],
) -> str:
    """Why `found` is not the recording that was asked for, or "".

    Split out from the lookup so the rules are testable without a network.
    """
    found_title = str(found.get("title") or "")
    found_artist = str(found.get("artist") or "")
    found_duration = int(found.get("duration_ms") or 0)

    # A karaoke, instrumental or live take of the right song under the right
    # name. strip_noise() erases the marker before titles_match() ever sees
    # it — "Like Him (feat. Lola Young)" and "Like Him … (Melody Karaoke
    # Version)" compare equal once stripped — so this has to be asked first.
    if expected_title and found_title and variant_conflict(expected_title, found_title):
        return f"different version: {found_title!r}"

    if (
        expected_artist
        and found_artist
        and not artists_match(expected_artist, found_artist)
    ):
        return f"different artist: {found_artist!r}"

    if expected_title and found_title and not titles_match(expected_title, found_title):
        return f"different title: {found_title!r}"

    if (
        expected_duration_ms
        and found_duration
        and abs(expected_duration_ms - found_duration) > DURATION_TOLERANCE_MS
    ):
        return (
            f"different length: {found_duration // 1000}s, "
            f"expected {expected_duration_ms // 1000}s"
        )

    # The clean edit. Same title, same artist, same length to the second —
    # the only thing that separates it from the recording that was asked for
    # is that the words are muted, and this flag is the only place that
    # difference is written down.
    if expected_explicit and not found.get("explicit"):
        return "clean edit of an explicit track"

    return ""


async def wrong_recording_reason_async(
    *,
    requested_isrc: str,
    resolved_isrc: str,
    title: str,
    artist: str,
    duration_ms: int,
    is_explicit: bool,
) -> str:
    """Why the provider's recording is not the requested one, or "".

    `requested_isrc` is what the track was asked for and `resolved_isrc` is
    what the provider wrote back. Equal (or either one missing) means there
    is nothing to check.
    """
    want = normalize_isrc(requested_isrc)
    got = normalize_isrc(resolved_isrc)
    if not want or not got or want == got:
        return ""

    found = await _lookup_isrc_async(got)
    if not found:
        logger.debug(
            "[recording-guard] %s → %s: no second opinion, accepting", want, got
        )
        return ""

    reason = judge_resolved_recording(
        expected_title=title,
        expected_artist=artist,
        expected_duration_ms=duration_ms,
        expected_explicit=is_explicit,
        found=found,
    )
    if reason:
        return f"provider resolved ISRC {got} instead of {want} — {reason}"
    return ""

"""SpotiFLAC/core/local_matcher.py — Phase 2: Match Engine.

Connects a locally-scanned file (local_scanner.LocalFileInfo) to a proper
TrackMetadata match, by reusing SpotifyMetadataClient.search() — the exact
same search engine already used by the GUI's search tab (see
app.py:search_provider) — rather than building a second one.

Two things decide a match here, in order of how much they can be trusted:

1. The file's own ISRC, when it has one. An ISRC names a specific recording,
   so this is an identity check rather than a guess — no amount of string
   similarity beats it, and files that were themselves bought or downloaded
   from a store usually carry one.
2. Otherwise, text similarity scored by core/text_match.py, the same scorer
   the CSV importer uses. This module used to carry its own, which glued
   "artist title" into a single string and compared that; see text_match's
   docstring for the two ways that misfires and why it was replaced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .isrc_utils import normalize_isrc
from .text_match import ratio, score_track_match, variant_conflict

if TYPE_CHECKING:
    from .local_scanner import LocalFileInfo
    from .models import TrackMetadata

logger = logging.getLogger(__name__)

#: At or above this the UI pre-selects the match, so applying is one click
#: with no per-file review (see renderLocalTracks() in frontend/app.js).
#: That makes it a threshold for *writing to someone's files unattended*,
#: which is why it sits well above "probably right".
SAFE_MATCH_THRESHOLD = 90.0

#: A file's own ISRC identifies the recording outright, so a candidate that
#: carries the same one is not scored against the text at all.
ISRC_MATCH_CONFIDENCE = 100.0

#: Independently of the overall score, the *titles* have to agree this well
#: before a match may be applied unattended. Without it a long identical
#: artist name carries 0.4 of the weight on its own, which is enough to push
#: a genuinely different song over the line: "Red Hot Chili Peppers - Can't
#: Stop" scored 88 against "…- Don't Stop", two points short, on a title
#: ratio of 0.80. A wrong title is the one thing that must not be absorbed
#: by the rest of the score.
SAFE_TITLE_RATIO = 0.9


@dataclass
class MatchCandidate:
    metadata: TrackMetadata
    confidence: float  # 0-100
    #: How this candidate was decided: "isrc" (identity) or "text"
    #: (similarity). Worth keeping so the UI can say *why* a row is
    #: trusted rather than only how much.
    how: str = "text"
    #: Title agreement on its own, 0…1, kept out of the aggregate so
    #: is_safe can require it separately. See SAFE_TITLE_RATIO.
    title_ratio: float = 1.0
    #: The candidate is a live take / remix / instrumental where the file is
    #: not (or vice versa), and no duration was available to confirm it.
    variant_unconfirmed: bool = False
    #: Whether the file gave us an artist at all — a tag, or a filename the
    #: guesser could split. Without one the score is the title agreement
    #: alone, which for a file called "01.mp3" means searching for "01" and
    #: scoring 100 against any track that happens to be titled "01".
    artist_known: bool = True

    @property
    def is_safe(self) -> bool:
        """Whether this may be written to the file without the user first
        confirming it — the UI pre-ticks these, so the bar is deliberately
        higher than "most likely correct".
        """
        if self.how == "isrc":
            return True
        return (
            self.confidence >= SAFE_MATCH_THRESHOLD
            and self.title_ratio >= SAFE_TITLE_RATIO
            and not self.variant_unconfirmed
            and self.artist_known
        )


def score_match(
    title: str,
    artist: str,
    candidate: TrackMetadata,
    *,
    album: str = "",
    duration_ms: int = 0,
) -> float:
    """Confidence score (0-100) for how well `candidate` matches the file.

    Title and artist are scored separately and weighted (see
    text_match.score_track_match); album and duration adjust the result when
    the file has them. Still a heuristic over metadata, not over the audio —
    good enough to separate "safe to auto-apply" from "ask the user", not a
    guarantee.
    """
    return round(
        100.0
        * score_track_match(
            title=title,
            artist=artist,
            album=album,
            duration_ms=duration_ms,
            candidate=candidate,
        ),
        1,
    )


async def search_and_match(
    title: str,
    artist: str,
    album: str | None = None,
    *,
    limit: int = 10,
    duration_ms: int = 0,
    isrc: str = "",
    client=None,
) -> list[MatchCandidate]:
    """Searches for (title, artist[, album]) and returns candidates sorted by
    confidence, best first. Returns an empty list if nothing is found or the
    search itself fails — callers should treat that as "no match", not crash.

    When `isrc` is given and a returned candidate carries the same one, that
    candidate is promoted to the front at full confidence: the two are the
    same recording by definition, whatever the titles happen to say.

    `client` is an optional SpotifyMetadataClient to reuse. Constructing one
    costs a session bootstrap — measured at ~640 ms — so a caller matching a
    whole folder should build one and pass it in for every file rather than
    letting each call make its own; scan_and_match_async() does. Omitted, one
    is created here, which keeps single-file callers working unchanged.
    """
    if not title and not artist:
        return []

    query = " ".join(p for p in (artist, title) if p).strip()

    try:
        import asyncio

        if client is None:
            from .spotify_metadata import SpotifyMetadataClient

            client = SpotifyMetadataClient()
        results = await asyncio.to_thread(client.search, query, limit)
    except Exception as exc:
        logger.warning("[local_matcher] search failed for %r: %s", query, exc)
        return []

    tracks = results.get("tracks", []) if isinstance(results, dict) else []
    candidates = []
    for t in tracks:
        candidate_title = getattr(t, "title", "")
        candidate_duration = int(getattr(t, "duration_ms", 0) or 0)
        # A live/remix/instrumental marker only counts against the match when
        # there is no duration to settle it: two running times that agree are
        # better evidence than a word in a title, and the score already
        # penalises two that do not.
        durations_known = bool(duration_ms and candidate_duration)
        candidates.append(
            MatchCandidate(
                metadata=t,
                confidence=score_match(
                    title, artist, t, album=album or "", duration_ms=duration_ms
                ),
                title_ratio=ratio(title, candidate_title),
                variant_unconfirmed=(
                    not durations_known and variant_conflict(title, candidate_title)
                ),
                artist_known=bool(artist),
            )
        )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    candidates = candidates[:limit]

    # search() above is Spotify's lightweight search — it deliberately leaves
    # several fields blank (release_date, composer, copyright, isrc,
    # track_number, disc_number, total_tracks) and its cover art is often
    # lower-resolution than the dedicated track endpoint. The local
    # auto-tagger UI only ever previews/applies candidates[0], so fetch full
    # details for that one match (same call the normal search→download flow
    # already makes) instead of leaving it at search-result quality.
    if candidates:
        try:
            full = await client.get_track_async(candidates[0].metadata.id)
            candidates[0] = MatchCandidate(
                metadata=full,
                confidence=candidates[0].confidence,
                how=candidates[0].how,
                title_ratio=candidates[0].title_ratio,
                variant_unconfirmed=candidates[0].variant_unconfirmed,
                artist_known=candidates[0].artist_known,
            )
        except Exception as exc:
            logger.debug(
                "[local_matcher] full-detail fetch failed for %r, keeping "
                "search-result metadata: %s",
                candidates[0].metadata.id,
                exc,
            )

    # The ISRC check runs last, on purpose: `search()` does not populate the
    # isrc field, so only the fully-fetched best candidate above has one to
    # compare. A hit here overrides whatever the text scoring concluded.
    wanted = normalize_isrc(isrc)
    if wanted:
        for i, cand in enumerate(candidates):
            if normalize_isrc(getattr(cand.metadata, "isrc", "")) == wanted:
                candidates[i] = MatchCandidate(
                    metadata=cand.metadata,
                    confidence=ISRC_MATCH_CONFIDENCE,
                    how="isrc",
                    title_ratio=cand.title_ratio,
                    artist_known=cand.artist_known,
                )
                candidates.insert(0, candidates.pop(i))
                break

    return candidates


async def match_local_file(
    info: LocalFileInfo, *, limit: int = 5, client=None
) -> list[MatchCandidate]:
    """Convenience wrapper: matches a scanned LocalFileInfo using whichever
    title/artist it has — real tags if present, filename guess otherwise
    (see LocalFileInfo.search_title / .search_artist) — plus its ISRC and
    running time when the file carries them.
    """
    return await search_and_match(
        info.search_title,
        info.search_artist,
        info.old_album or None,
        limit=limit,
        duration_ms=getattr(info, "old_duration_ms", 0),
        isrc=getattr(info, "old_isrc", ""),
        client=client,
    )

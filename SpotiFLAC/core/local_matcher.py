"""SpotiFLAC/core/local_matcher.py — Phase 2: Match Engine.

Connects a locally-scanned file (local_scanner.LocalFileInfo) to a proper
TrackMetadata match, by reusing SpotifyMetadataClient.search() — the exact
same search engine already used by the GUI's search tab (see
app.py:search_provider) — rather than building a second one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .local_scanner import LocalFileInfo
    from .models import TrackMetadata

logger = logging.getLogger(__name__)

SAFE_MATCH_THRESHOLD = 90.0


@dataclass
class MatchCandidate:
    metadata: TrackMetadata
    confidence: float  # 0-100

    @property
    def is_safe(self) -> bool:
        return self.confidence >= SAFE_MATCH_THRESHOLD


def _normalize(s: str) -> str:
    return " ".join(s.lower().strip().split())


def score_match(title: str, artist: str, candidate: TrackMetadata) -> float:
    """Confidence score (0-100) for how well `candidate` matches the given
    (title, artist), by comparing normalized title+artist strings.

    A pure string-similarity heuristic — it doesn't know anything about the
    actual audio, only about how closely the text matches. Good enough to
    separate "safe to auto-apply" from "ask the user", not a guarantee.
    """
    query = _normalize(f"{artist} {title}")
    candidate_str = _normalize(f"{candidate.first_artist} {candidate.title}")
    ratio = SequenceMatcher(None, query, candidate_str).ratio()
    return round(ratio * 100, 1)


async def search_and_match(
    title: str,
    artist: str,
    album: str | None = None,
    *,
    limit: int = 10,
) -> list[MatchCandidate]:
    """Searches for (title, artist[, album]) and returns candidates sorted by
    confidence, best first. Returns an empty list if nothing is found or the
    search itself fails — callers should treat that as "no match", not crash.
    """
    if not title and not artist:
        return []

    from .spotify_metadata import SpotifyMetadataClient

    query = " ".join(p for p in (artist, title) if p).strip()

    try:
        import asyncio

        client = SpotifyMetadataClient()
        results = await asyncio.to_thread(client.search, query, limit)
    except Exception as exc:
        logger.warning("[local_matcher] search failed for %r: %s", query, exc)
        return []

    tracks = results.get("tracks", []) if isinstance(results, dict) else []
    candidates = [
        MatchCandidate(metadata=t, confidence=score_match(title, artist, t))
        for t in tracks
    ]

    if album:
        norm_album = _normalize(album)
        for c in candidates:
            if _normalize(c.metadata.album) == norm_album:
                c.confidence = min(100.0, c.confidence + 5.0)

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates[:limit]


async def match_local_file(
    info: LocalFileInfo, *, limit: int = 5
) -> list[MatchCandidate]:
    """Convenience wrapper: matches a scanned LocalFileInfo using whichever
    title/artist it has — real tags if present, filename guess otherwise
    (see LocalFileInfo.search_title / .search_artist).
    """
    return await search_and_match(
        info.search_title,
        info.search_artist,
        info.old_album or None,
        limit=limit,
    )

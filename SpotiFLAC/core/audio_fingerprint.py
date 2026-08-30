"""Acoustic-fingerprint duplicate detection for the local library tools.

Local Tagging's existing dedup (see local_matcher.py / playlist_sync.py)
matches by ISRC or by normalized title+artist text. Both are cheap and
usually right, but neither looks at the audio itself: a re-rip with wrong
or missing tags, or the same recording from two providers with slightly
different metadata, can slip through as "not a duplicate" when it plainly
is one to a human ear.

This module adds a second, independent signal — Chromaprint acoustic
fingerprints (via the optional `pyacoustid` package, itself a thin wrapper
around the `fpcalc` binary from https://acoustid.org/chromaprint) — for
callers who want to catch those. It never talks to the network or the
AcoustID lookup service; everything here is a purely local, fingerprint-to-
fingerprint comparison, so it needs no API key and sends no data anywhere.

Public API:
    - is_available() -> bool
    - compute_fingerprint(path) -> AudioFingerprint      (sync, blocking)
    - compute_fingerprint_async(path) -> AudioFingerprint (off-thread)
    - fingerprint_similarity(a, b) -> float               (0.0-1.0)
    - find_duplicate_groups(fingerprints, ...) -> list[list[Path]]

Off by default and fully opt-in, same posture as core/hires_check.py:
requires `pip install SpotiFLAC[dedup]` (pyacoustid + the system `fpcalc`
binary); if either is missing, is_available() is False and callers are
expected to skip the feature rather than fail.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("SpotiFLAC.audio_fingerprint")

try:
    import acoustid

    _ACOUSTID_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depends on optional install
    acoustid = None  # type: ignore[assignment]
    _ACOUSTID_IMPORT_ERROR = exc

# Duplicate recordings of the same song are, in practice, always within a
# couple of seconds of each other in duration. Comparing durations first is
# O(1) per pair and skips the much more expensive fingerprint alignment
# below for everything that couldn't possibly match — without this, a
# library of a few thousand files makes pairwise comparison impractical.
DEFAULT_DURATION_TOLERANCE_S = 3.0

# Chromaprint fingerprints below this bit-similarity are treated as
# "different recording". 0.95 is conservative (deliberately biased toward
# missing a real duplicate over flagging two different tracks as one) —
# same design choice as hires_check's "hint, not certification" stance.
DEFAULT_SIMILARITY_THRESHOLD = 0.95


class AudioFingerprintError(Exception):
    """Raised by compute_fingerprint()/compute_fingerprint_async() on failure."""


@dataclass(frozen=True)
class AudioFingerprint:
    path: Path
    duration_s: float
    raw: tuple[int, ...]  # decoded 32-bit Chromaprint fingerprint ints
    #: The compressed, base64 form fpcalc actually emits. Kept because it is
    #: what AcoustID's /v2/lookup wants as its `fingerprint` parameter (see
    #: acoustid_lookup.py) — it used to be decoded and discarded, so
    #: identifying a file meant running fpcalc a second time for a value the
    #: first run had already produced.
    compressed: str = ""


def is_available() -> bool:
    """Whether fingerprinting can run at all: pyacoustid importable AND its
    underlying `fpcalc` binary on PATH. Mirrors ffmpeg_check.check_ffmpeg()'s
    approach (check PATH directly) rather than pyacoustid's own probing,
    which raises its own acoustid.NoBackendError — not a plain
    FileNotFoundError — making a "call it and see" check needlessly fragile.
    """
    if acoustid is None:
        return False
    return shutil.which("fpcalc") is not None


def compute_fingerprint(path: str | Path) -> AudioFingerprint:
    """Computes the Chromaprint fingerprint for one audio file. Raises
    AudioFingerprintError if `pyacoustid`/`fpcalc` aren't available or the
    file can't be decoded — callers should treat that as "skip this file",
    not abort a whole library scan.
    """
    if acoustid is None:
        msg = f"pyacoustid not installed ({_ACOUSTID_IMPORT_ERROR}); pip install SpotiFLAC[dedup]"
        raise AudioFingerprintError(msg)

    p = Path(path)
    try:
        duration, compressed = acoustid.fingerprint_file(str(p))
        raw_ints, _algorithm = acoustid.chromaprint.decode_fingerprint(compressed)
    except Exception as exc:
        msg = f"Could not fingerprint {p.name}: {exc}"
        raise AudioFingerprintError(msg) from exc

    return AudioFingerprint(
        path=p,
        duration_s=float(duration),
        raw=tuple(raw_ints),
        compressed=compressed.decode()
        if isinstance(compressed, bytes)
        else str(compressed),
    )


async def compute_fingerprint_async(path: str | Path) -> AudioFingerprint:
    """Off-thread wrapper — fpcalc is a blocking subprocess call."""
    import asyncio

    return await asyncio.to_thread(compute_fingerprint, path)


def _popcount(x: int) -> int:
    return bin(x & 0xFFFFFFFF).count("1")


def fingerprint_similarity(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    """Bit-level similarity (0.0-1.0) between two decoded fingerprints,
    trying every relative alignment offset and keeping the best one — the
    same idea libchromaprint's own comparison tool uses, since two
    recordings of the same song rarely start at the exact same sample
    (different silence padding, encoder delay, etc).

    Returns 0.0 for empty input or when no offset gives enough overlap to
    be meaningful (< half of the shorter fingerprint).
    """
    if not a or not b:
        return 0.0

    shorter = min(len(a), len(b))
    min_overlap = max(1, shorter // 2)
    best = 0.0

    for offset in range(-len(b) + 1, len(a)):
        if offset >= 0:
            pairs = list(zip(a[offset:], b))
        else:
            pairs = list(zip(a, b[-offset:]))
        if len(pairs) < min_overlap:
            continue
        bit_errors = sum(_popcount(x ^ y) for x, y in pairs)
        similarity = 1.0 - (bit_errors / (len(pairs) * 32))
        best = max(best, similarity)

    return best


def find_duplicate_groups(
    fingerprints: list[AudioFingerprint],
    *,
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[list[Path]]:
    """Groups files that are acoustically the same recording.

    O(n^2) comparisons in the worst case, but the duration pre-filter
    (`duration_tolerance_s`) skips the expensive alignment search for any
    pair that couldn't plausibly match, which is nearly every pair in a
    real, varied library — this stays practical well beyond a toy library
    size, though a library of many thousands of same-length tracks (a
    podcast feed, an audiobook split into equal chapters) is the pathological
    case where it would still be slow.

    Returns groups of 2+ paths that are the same recording; singletons
    (nothing else matched) are omitted, mirroring how a "review these
    duplicates" UI would want the result — nothing to show for a unique file.
    """
    n = len(fingerprints)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if (
                abs(fingerprints[i].duration_s - fingerprints[j].duration_s)
                > duration_tolerance_s
            ):
                continue
            if (
                fingerprint_similarity(fingerprints[i].raw, fingerprints[j].raw)
                >= similarity_threshold
            ):
                union(i, j)

    groups: dict[int, list[Path]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(fingerprints[i].path)

    return [g for g in groups.values() if len(g) > 1]

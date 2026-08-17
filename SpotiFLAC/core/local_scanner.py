"""SpotiFLAC/core/local_scanner.py — Phase 1: Local Scanner.

Reads local audio files and extracts what's needed to compare against a
fresh match: current tags, current cover art (as a data URI, so the
frontend can display it with no extra round-trip), and — when a file has
no usable tags at all — a best-effort guess at title/artist from the
filename.

Supported formats mirror exactly what tagger.py can write (FLAC, MP3,
M4A/AAC, OGG Vorbis, Opus, WAV, AIFF, WMA, WavPack, Monkey's Audio,
Musepack, TrueAudio) — see tagger.SUPPORTED_SUFFIXES, the single source of
truth both modules read from, so scanning and (re)tagging never drift out
of sync with each other again.

This module only reads. It never writes to a file; see local_processor.py
for the part that applies new tags.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .tagger import SUPPORTED_SUFFIXES, EmbeddedTags, read_embedded_tags

logger = logging.getLogger(__name__)

# Kept as an alias for backward compatibility with existing imports
# (`from .local_scanner import SUPPORTED_EXTENSIONS`) — the actual list now
# lives in tagger.py so it can never fall out of sync with what gets written.
SUPPORTED_EXTENSIONS = SUPPORTED_SUFFIXES

# "Artist - Title.ext", "Artist_-_Title.ext", "01. Artist - Title.ext", etc.
_FILENAME_PATTERN = re.compile(
    r"^(?:\d{1,3}[.\-_\s]+)?"  # optional leading track number
    r"(?P<artist>.+?)\s*[-–—_]\s*(?P<title>.+)$",
)


@dataclass
class LocalFileInfo:
    """Everything extracted from one local file, before any matching happens."""

    file_path: str
    old_title: str = ""
    old_artist: str = ""
    old_album: str = ""
    old_year: str = ""
    old_genre: str = ""
    old_isrc: str = ""
    old_cover_base64: str = ""  # data URI, e.g. "data:image/jpeg;base64,..."
    guessed_title: str = ""
    guessed_artist: str = ""
    has_tags: bool = False
    error: str = ""

    @property
    def search_title(self) -> str:
        """Best title to search with: real tag if present, else the filename guess."""
        return self.old_title or self.guessed_title

    @property
    def search_artist(self) -> str:
        """Best artist to search with: real tag if present, else the filename guess."""
        return self.old_artist or self.guessed_artist


def _guess_from_filename(path: Path) -> tuple[str, str]:
    """Task 4 (Fallback Parser): deduces (artist, title) from a filename like
    'Artist - Title.mp3' when the file itself carries no usable tags.
    Returns ("", "") if the filename doesn't match the expected shape —
    callers should treat that as "no guess available", not silently wrong.
    """
    stem = path.stem
    m = _FILENAME_PATTERN.match(stem)
    if not m:
        return "", ""
    artist = m.group("artist").strip().replace("_", " ")
    title = m.group("title").strip().replace("_", " ")
    return artist, title


def _apply_embedded_tags(embedded: EmbeddedTags, info: LocalFileInfo) -> None:
    """Maps the canonical (uppercase, Vorbis-style) tag keys that
    tagger.read_embedded_tags() returns for *every* supported format onto
    the LocalFileInfo fields the UI/matcher care about.
    """
    tags = embedded.tags

    def _get(*keys: str) -> str:
        for key in keys:
            val = tags.get(key)
            if val:
                return str(val)
        return ""

    info.old_title = _get("TITLE")
    info.old_artist = _get("ARTIST")
    info.old_album = _get("ALBUM")
    info.old_year = _get("DATE", "YEAR", "ORIGINALDATE", "ORIGINALYEAR")[:4]
    info.old_genre = _get("GENRE")
    info.old_isrc = _get("ISRC")
    info.has_tags = bool(info.old_title or info.old_artist)

    if embedded.cover_data:
        mime = embedded.cover_mime or "image/jpeg"
        import base64

        b64 = base64.b64encode(embedded.cover_data).decode("ascii")
        info.old_cover_base64 = f"data:{mime};base64,{b64}"


def scan_file(path: str | Path) -> LocalFileInfo:
    """Reads one audio file. Never raises — a file that can't be read comes
    back with `.error` set and everything else empty, so a batch scan can't
    be aborted by one bad file.
    """
    p = Path(path)
    info = LocalFileInfo(file_path=str(p))

    if not p.exists():
        info.error = "File not found"
        return info

    suffix = p.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        info.error = f"Unsupported format: {suffix}"
        return info

    try:
        embedded = read_embedded_tags(p)
        _apply_embedded_tags(embedded, info)
    except Exception as exc:
        logger.warning("[local_scanner] failed to read %s: %s", p.name, exc)
        info.error = f"Could not read tags: {exc}"

    if not info.has_tags:
        artist, title = _guess_from_filename(p)
        info.guessed_artist = artist
        info.guessed_title = title

    return info


def scan_path(path: str | Path, *, recursive: bool = True) -> list[LocalFileInfo]:
    """Scans a single file or every supported audio file under a directory.

    Files that raise on read are still included in the result (with `.error`
    set) rather than dropped, so the caller/UI can show *something* for every
    file that was found instead of silently skipping it.
    """
    p = Path(path)

    if p.is_file():
        return [scan_file(p)]

    if not p.is_dir():
        return [LocalFileInfo(file_path=str(p), error="Path not found")]

    pattern = "**/*" if recursive else "*"
    files = sorted(
        f
        for f in p.glob(pattern)
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    return [scan_file(f) for f in files]
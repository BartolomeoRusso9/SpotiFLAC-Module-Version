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

# Common local filename shapes: "Artist - Title.ext",
# "Title - Artist.ext", "Artist_-_Title.ext", "01. Artist - Title.ext", etc.
_FILENAME_PATTERN = re.compile(
    r"^(?:\d{1,3}[.\-_\s]+)?"  # optional leading track number
    r"(?P<artist>.+?)\s*[-–—_]\s*(?P<title>.+)$",
)
_FILENAME_TITLE_FIRST_PATTERN = re.compile(
    r"^(?:\d{1,3}[.\-_\s]+)?"  # optional leading track number
    r"(?P<title>.+?)\s*[-–—_]\s*(?P<artist>.+)$",
)


def _looks_like_artist(value: str) -> bool:
    """Heuristic for whether a filename segment is more likely an artist name."""
    text = value.strip()
    if not text:
        return False
    lower = text.lower()
    if any(token in lower for token in (" feat", " ft.", " ft", "&", "/", ",")):
        return True
    words = re.split(r"[\s_]+", text)
    return len(words) > 1 and not any(ch.isdigit() for ch in text)


def _filename_guess_score(artist: str, title: str) -> int:
    """Higher score means more likely to be the correct artist/title split."""
    score = 0
    if _looks_like_artist(artist):
        score += 5
    if not _looks_like_artist(title):
        score += 2
    if len(title.split()) <= 3 and len(artist.split()) > 1:
        score += 2
    if len(artist.split()) > len(title.split()):
        score += 1
    return score


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
    """Fallback parser for files that have no embedded metadata.

    Files named like 'Artist - Title.flac' are parsed into both artist and title.
    We also support title-first variants such as 'Ouverture - Lazza, Low Kidd.flac'
    without losing the valid plain filename fallback for simple names like
    'plain_track.flac'.
    """
    stem = path.stem
    candidates: list[tuple[str, str]] = []

    for pattern in (_FILENAME_PATTERN, _FILENAME_TITLE_FIRST_PATTERN):
        match = pattern.match(stem)
        if not match:
            continue

        artist = match.group("artist").strip().replace("_", " ")
        title = match.group("title").strip().replace("_", " ")
        if artist and title:
            candidates.append((artist, title))

    if candidates:
        best_artist, best_title = max(
            candidates,
            key=lambda pair: _filename_guess_score(*pair),
        )

        # A single underscore or dash between two short words is often just a
        # plain filename like 'plain_track' rather than a real artist/title split.
        separator_count = sum(stem.count(ch) for ch in "_-–—")
        if (
            separator_count <= 1
            and " " not in stem
            and not any(ch in stem for ch in " -–—")
        ):
            cleaned = re.sub(r"[_\-.]+", " ", stem).strip()
            return "", cleaned if cleaned else ""

        return best_artist, best_title

    cleaned = re.sub(r"[_\-.]+", " ", stem).strip()
    if not cleaned:
        return "", ""
    return "", cleaned


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

    if p.is_dir():
        pattern = "**/*" if recursive else "*"
        files = sorted(
            f
            for f in p.glob(pattern)
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        return [scan_file(f) for f in files]

    # Path doesn't exist or is neither file nor directory
    if not p.exists():
        return [LocalFileInfo(file_path=str(p), error=f"Path does not exist: {p}")]

    return [
        LocalFileInfo(
            file_path=str(p), error=f"Path is neither file nor directory: {p}"
        )
    ]

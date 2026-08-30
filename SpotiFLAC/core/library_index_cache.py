"""SpotiFLAC/core/library_index_cache.py — don't re-read the whole library.

playlist_sync.index_audio_files() answers "do I already have this track?"
by walking the output directory and reading the embedded tags of every
audio file it finds. It is called before a download run, and it reads every
file every time — so the cost of deciding whether to download one track
grows with the size of the library it is going into.

Tags only change when the file does, and a file that changed says so in its
mtime and size. Remembering the parse per path and re-reading only what
moved turns the walk from "open and decode every file" into "stat every
file", which is what makes the check cheap enough to stop noticing.

Ported in spirit from the mobile app's library_scan_incremental.go, which
keeps the same path→mtime snapshot for the same reason.

The cache is advisory. A corrupt, unreadable or stale entry costs a re-read
and nothing else, so every failure path here falls back to parsing rather
than to an error — a wrong answer from this module would mean skipping a
download the user asked for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Bumped when the stored shape changes, so an old cache is ignored rather
#: than misread.
_VERSION = 1

_CACHE_DIR = Path.home() / ".cache" / "spotiflac" / "library-index"


@dataclass(frozen=True)
class CachedTags:
    """The parts of a file's tags the index actually keys on."""

    isrc: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""


def cache_path_for(output_dir: Path | str) -> Path:
    """Where one directory's cache lives.

    Hashed rather than derived from the path so that a long or unusual
    directory name cannot produce an invalid filename.
    """
    key = hashlib.sha256(
        os.path.normcase(os.path.abspath(str(output_dir))).encode("utf-8")
    ).hexdigest()[:32]
    return _CACHE_DIR / f"{key}.json"


def _stamp(path: Path) -> str:
    """A file's identity for cache purposes: mtime and size.

    Not a checksum. Hashing every file would cost exactly what this module
    exists to avoid, and a tag edit that leaves both the mtime and the size
    untouched is not something any tagger does.
    """
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{int(stat.st_mtime_ns)}:{stat.st_size}"


class LibraryIndexCache:
    """Remembers what each file's tags said, keyed by path and stamp."""

    def __init__(self, output_dir: Path | str) -> None:
        self.path = cache_path_for(output_dir)
        self._entries: dict[str, dict] = {}
        self._seen: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.debug("[library-index] cache unreadable, rebuilding: %s", exc)
            return
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self._entries = entries

    def get(self, path: Path) -> CachedTags | None:
        """The remembered tags for `path`, or None if it must be re-read."""
        stamp = _stamp(path)
        if not stamp:
            return None
        entry = self._entries.get(str(path))
        if not isinstance(entry, dict) or entry.get("stamp") != stamp:
            self.misses += 1
            return None
        self.hits += 1
        self._seen[str(path)] = entry
        return CachedTags(
            isrc=str(entry.get("isrc") or ""),
            title=str(entry.get("title") or ""),
            artist=str(entry.get("artist") or ""),
            album=str(entry.get("album") or ""),
        )

    def put(self, path: Path, tags: CachedTags) -> None:
        """Records a fresh parse."""
        stamp = _stamp(path)
        if not stamp:
            return
        self._seen[str(path)] = {
            "stamp": stamp,
            "isrc": tags.isrc,
            "title": tags.title,
            "artist": tags.artist,
            "album": tags.album,
        }

    def save(self) -> None:
        """Writes back only what this walk saw.

        Dropping unseen entries is what keeps the file from growing forever
        as tracks are renamed, moved or deleted — the walk that just
        finished is the definition of what exists.
        """
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            from .atomic_io import write_json_atomic

            write_json_atomic(self.path, {"version": _VERSION, "entries": self._seen})
        except Exception as exc:
            # A cache that cannot be written is a cache that is not used.
            logger.debug("[library-index] could not save cache: %s", exc)

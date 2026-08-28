"""Inspecting and pruning the on-disk caches (`spotiflac --cache ...`).

Nothing here ever removed anything. `response_cache` checks its TTL on
*read*, so an entry nobody asks for again is never noticed and never
deleted: the directory only grows. `isrc-cache.json` and
`recent-fetches.json` are capped by nothing at all.

None of that is urgent — these are small JSON files — but "a directory
under $HOME that only grows, with no way to see how big it is or empty it"
is a thing a long-running install eventually notices, and the fix is a
handful of functions.

What is and isn't a cache
-------------------------
`profiles.json` and `gui-settings.json` live in the same directory but are
*configuration*, not cache: losing them loses something the user typed.
They are reported for completeness and never pruned, not even by
`--cache-clear`.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


#: Same resolution response_cache and provider_stats use, so an instance with
#: SPOTIFLAC_CACHE_DIR set reports and prunes the directory it actually uses.
def cache_root() -> Path:
    return Path(
        os.getenv("SPOTIFLAC_CACHE_DIR", str(Path.home() / ".cache" / "spotiflac"))
    )


#: Files that are genuinely disposable: deleting them costs a re-fetch.
PRUNABLE_FILES = (
    "isrc-cache.json",
    "recent-fetches.json",
    "provider_priority.json",  # core/provider_stats.py
    "session.json",
)

#: Configuration that happens to sit in the cache directory. Never pruned.
PRESERVED_FILES = (
    "profiles.json",
    "gui-settings.json",
)

RESPONSES_DIR = "responses"

#: Cached HTTP responses older than this are stale by any reasonable
#: definition — the longest TTL any caller passes is well under a day.
DEFAULT_MAX_AGE_S = 7 * 24 * 3600


@dataclass
class CacheEntry:
    name: str
    path: Path
    size_bytes: int
    files: int
    prunable: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "files": self.files,
            "prunable": self.prunable,
        }


def _dir_size(path: Path) -> tuple[int, int]:
    total = count = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
                count += 1
        except OSError:
            continue
    return total, count


def stats() -> dict:
    """What is on disk, broken down by entry. Never modifies anything."""
    root = cache_root()
    entries: list[CacheEntry] = []

    if root.is_dir():
        responses = root / RESPONSES_DIR
        if responses.is_dir():
            size, count = _dir_size(responses)
            entries.append(
                CacheEntry(RESPONSES_DIR, responses, size, count, prunable=True)
            )

        for name in (*PRUNABLE_FILES, *PRESERVED_FILES):
            path = root / name
            if path.is_file():
                entries.append(
                    CacheEntry(
                        name,
                        path,
                        path.stat().st_size,
                        1,
                        prunable=name in PRUNABLE_FILES,
                    )
                )

    return {
        "root": str(root),
        "exists": root.is_dir(),
        "total_bytes": sum(e.size_bytes for e in entries),
        "prunable_bytes": sum(e.size_bytes for e in entries if e.prunable),
        "entries": [e.to_dict() for e in entries],
    }


def prune(max_age_s: float = DEFAULT_MAX_AGE_S, dry_run: bool = False) -> dict:
    """Deletes cached HTTP responses older than `max_age_s`.

    Only touches `responses/` — the other files are single documents that are
    either current or absent, not accumulations. `dry_run` reports what would
    go without removing it, because "delete things under $HOME" deserves a
    way to look first.
    """
    # A negative age puts the cutoff in the future and deletes the whole
    # cache; NaN makes every comparison false and deletes nothing, silently.
    # Both come from a --cache-max-age-days the user typed, so say which it
    # was rather than doing something surprising.
    if not math.isfinite(max_age_s) or max_age_s < 0:
        msg = f"max_age_s must be a non-negative number, got {max_age_s!r}"
        raise ValueError(msg)

    root = cache_root()
    responses = root / RESPONSES_DIR
    removed = freed = 0
    cutoff = time.time() - max_age_s

    if responses.is_dir():
        for path in responses.rglob("*.json"):
            try:
                stat = path.stat()
                if stat.st_mtime > cutoff:
                    continue
                freed += stat.st_size
                removed += 1
                if not dry_run:
                    path.unlink()
            except OSError:
                continue

        if not dry_run:
            # Namespace directories left empty by the sweep.
            for directory in sorted(
                (p for p in responses.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass  # not empty, or in use — fine either way

    return {
        "root": str(root),
        "dry_run": dry_run,
        "max_age_s": max_age_s,
        "removed_files": removed,
        "freed_bytes": freed,
    }


def clear(dry_run: bool = False) -> dict:
    """Removes every disposable cache, keeping configuration.

    Deliberately not `rmtree(cache_root())`: profiles.json and
    gui-settings.json live in the same directory and are things the user
    typed, not things SpotiFLAC can re-fetch.
    """
    root = cache_root()
    removed: list[str] = []
    freed = 0

    if root.is_dir():
        responses = root / RESPONSES_DIR
        if responses.is_dir():
            size, _ = _dir_size(responses)
            freed += size
            removed.append(RESPONSES_DIR)
            if not dry_run:
                shutil.rmtree(responses, ignore_errors=True)

        for name in PRUNABLE_FILES:
            path = root / name
            if path.is_file():
                freed += path.stat().st_size
                removed.append(name)
                if not dry_run:
                    try:
                        path.unlink()
                    except OSError:
                        continue

    return {
        "root": str(root),
        "dry_run": dry_run,
        "removed": removed,
        "preserved": [n for n in PRESERVED_FILES if (root / n).is_file()],
        "freed_bytes": freed,
    }


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_stats(data: dict) -> str:
    """Human-readable rendering of stats(), for the CLI."""
    if not data["exists"]:
        return f"No cache directory at {data['root']}"

    lines = [f"Cache: {data['root']}", ""]
    width = max((len(e["name"]) for e in data["entries"]), default=0)
    for entry in data["entries"]:
        note = "" if entry["prunable"] else "  (configuration — never pruned)"
        lines.append(
            f"  {entry['name']:<{width}}  {human_bytes(entry['size_bytes']):>9}"
            f"  {entry['files']:>5} file(s){note}"
        )
    lines += [
        "",
        f"  {'total':<{width}}  {human_bytes(data['total_bytes']):>9}",
        f"  {'reclaimable':<{width}}  {human_bytes(data['prunable_bytes']):>9}",
    ]
    return "\n".join(lines)


def to_json(data: dict) -> str:
    return json.dumps(data, indent=2)

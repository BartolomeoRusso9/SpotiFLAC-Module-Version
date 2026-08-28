"""Machine-readable run reports (`--json`).

Everything the CLI prints today is written for a person: aligned columns, a
progress bar, colour, a summary paragraph. Anything scripting SpotiFLAC has
had to scrape that, which breaks the first time a label is reworded.

This collects the same information as data. It is built on the post-download
hook mechanism rather than threaded through the downloader separately — a
report *is* a hook that remembers what it was told, so `--json` and a
user-supplied `--post-hook` see exactly the same events.

Console output goes to stderr (see core/console._write), so stdout carries
only the JSON document and `spotiflac ... --json | jq` works as-is.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class TrackRecord:
    """One track's outcome, as data."""

    id: str
    title: str
    artists: str
    album: str
    isrc: str
    duration_ms: int
    status: str  # "downloaded" | "skipped" | "failed"
    provider: str | None = None
    format: str | None = None
    file_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "isrc": self.isrc,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "provider": self.provider,
            "format": self.format,
            "file_path": self.file_path,
            "error": self.error,
        }


class RunReport:
    """Collects TrackRecords as a run proceeds. Safe to use as a hook.

    Thread-safe because hooks for concurrent downloads can be invoked from
    the worker threads `run_hooks` offloads synchronous hooks to.
    """

    #: Bumped when the JSON shape changes incompatibly, so a script can
    #: refuse a document it doesn't understand rather than misread it.
    SCHEMA_VERSION = 1

    def __init__(self) -> None:
        self.records: list[TrackRecord] = []
        self.started_at = time.time()
        self._lock = threading.Lock()

    # Deliberately the hook signature: (result, metadata).
    def __call__(self, result: Any, metadata: Any) -> None:
        self.record(result, metadata)

    def record(self, result: Any, metadata: Any) -> None:
        if getattr(result, "skipped", False):
            status = "skipped"
        elif getattr(result, "success", False):
            status = "downloaded"
        else:
            status = "failed"

        entry = TrackRecord(
            id=str(getattr(metadata, "id", "") or ""),
            title=str(getattr(metadata, "title", "") or ""),
            artists=str(getattr(metadata, "artists", "") or ""),
            album=str(getattr(metadata, "album", "") or ""),
            isrc=str(getattr(metadata, "isrc", "") or ""),
            duration_ms=int(getattr(metadata, "duration_ms", 0) or 0),
            status=status,
            provider=getattr(result, "provider", None),
            format=getattr(result, "format", None),
            file_path=getattr(result, "file_path", None),
            error=getattr(result, "error", None),
        )
        with self._lock:
            self.records.append(entry)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            records = list(self.records)
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        for record in records:
            counts[record.status] = counts.get(record.status, 0) + 1
        return {
            "schema_version": self.SCHEMA_VERSION,
            "started_at": self.started_at,
            "finished_at": time.time(),
            "summary": {"total": len(records), **counts},
            "tracks": [record.to_dict() for record in records],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @property
    def failed(self) -> list[TrackRecord]:
        with self._lock:
            return [r for r in self.records if r.status == "failed"]

    def downloaded_paths(self) -> list[str]:
        """Files this run actually produced, in the order they finished."""
        with self._lock:
            return [
                r.file_path
                for r in self.records
                if r.status == "downloaded" and r.file_path
            ]

    def to_m3u(self, playlist_path: Any) -> str:
        """Extended M3U of everything downloaded, paths relative to the
        playlist file so the folder stays portable when moved or synced.

        Reuses core/playlist_sync's renderer rather than formatting M3U a
        second time — the multi-playlist sync path has produced these for a
        while and there is no reason for two spellings of the same format.
        """
        from pathlib import Path

        from .playlist_sync import M3UEntry, render_m3u

        with self._lock:
            entries = [
                M3UEntry(
                    path=Path(r.file_path),
                    title=r.title,
                    artists=r.artists,
                    duration_s=round(r.duration_ms / 1000) if r.duration_ms else 0,
                )
                for r in self.records
                if r.status == "downloaded" and r.file_path
            ]
        return render_m3u(entries, Path(playlist_path))

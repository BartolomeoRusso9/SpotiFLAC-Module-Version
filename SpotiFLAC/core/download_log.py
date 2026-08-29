"""core/download_log.py — a durable record of what was actually downloaded.

Nothing used to write this down. `recent-fetches.json` remembers what was
*looked up* (and only the last 50), `RunReport` remembers one run and is
thrown away with the process, and the filesystem remembers the files but not
who fetched them, from which provider, or when.

Three features need that record:

  - **Quotas** (`core/web_users.py`, `--web-multiuser`): "how much has this
    account downloaded today" is a question about history, not about disk.
  - **Subscriptions** (`core/subscriptions.py`): a release already fetched
    once should not come back the next time an artist is checked.
  - Simply answering "have I got this already?" across moved or retagged
    files, by ISRC rather than by path.

Written from a post-download hook (see `record_hook`), so it observes exactly
the same per-track events `--post-hook` and `--json` do, rather than the
downloader growing another notion of "tell me about each track".
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from . import db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadRecord:
    id: int
    owner: str
    spotify_id: str
    isrc: str
    title: str
    artist: str
    album: str
    provider: str
    file_path: str
    format: str
    bytes: int
    success: bool
    downloaded_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner": self.owner,
            "spotify_id": self.spotify_id,
            "isrc": self.isrc,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "provider": self.provider,
            "file_path": self.file_path,
            "format": self.format,
            "bytes": self.bytes,
            "success": self.success,
            "downloaded_at": self.downloaded_at,
        }


def _row_to_record(row) -> DownloadRecord:
    return DownloadRecord(
        id=int(row["id"]),
        owner=row["owner"] or "",
        spotify_id=row["spotify_id"] or "",
        isrc=row["isrc"] or "",
        title=row["title"] or "",
        artist=row["artist"] or "",
        album=row["album"] or "",
        provider=row["provider"] or "",
        file_path=row["file_path"] or "",
        format=row["format"] or "",
        bytes=int(row["bytes"] or 0),
        success=bool(row["success"]),
        downloaded_at=float(row["downloaded_at"] or 0.0),
    )


def record(
    *,
    owner: str = "",
    spotify_id: str = "",
    isrc: str = "",
    title: str = "",
    artist: str = "",
    album: str = "",
    provider: str = "",
    file_path: str = "",
    fmt: str = "",
    size_bytes: int | None = None,
    success: bool = True,
) -> None:
    """Appends one row. Never raises.

    A download that has already landed on disk must not be turned into a
    failure by a bookkeeping problem, so every error here is logged and
    swallowed — the same contract `run_hooks()` gives every other hook.
    """
    if size_bytes is None:
        size_bytes = _file_size(file_path)
    try:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO downloads (
                    owner, spotify_id, isrc, title, artist, album,
                    provider, file_path, format, bytes, success, downloaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
                    spotify_id,
                    (isrc or "").upper(),
                    title,
                    artist,
                    album,
                    provider,
                    file_path,
                    fmt or "",
                    int(size_bytes or 0),
                    1 if success else 0,
                    time.time(),
                ),
            )
    except Exception:
        logger.debug("[download_log] could not record %s", file_path, exc_info=True)


def _file_size(path: str) -> int:
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def record_hook(owner: str = "") -> Any:
    """A post-download hook that writes each finished track to the log.

    Usage mirrors `RunReport`: the returned callable is appended to
    `DownloadOptions.post_download_hooks`, so it is called with the same
    `(result, metadata)` every user hook gets.
    """

    def _on_track(result: Any, metadata: Any) -> None:
        record(
            owner=owner,
            spotify_id=getattr(metadata, "id", "") or "",
            isrc=getattr(metadata, "isrc", "") or "",
            title=getattr(metadata, "title", "") or "",
            artist=getattr(metadata, "artists", "") or "",
            album=getattr(metadata, "album", "") or "",
            provider=getattr(result, "provider", "") or "",
            file_path=getattr(result, "file_path", "") or "",
            fmt=getattr(result, "format", "") or "",
            success=bool(getattr(result, "success", False)),
        )

    _on_track.__qualname__ = "download_log.record_hook.on_track"
    return _on_track


def count_since(owner: str, since: float) -> int:
    """Successful tracks `owner` has downloaded since the given timestamp."""
    try:
        row = (
            db.connection()
            .execute(
                "SELECT COUNT(*) AS n FROM downloads "
                "WHERE owner = ? AND success = 1 AND downloaded_at >= ?",
                (owner, since),
            )
            .fetchone()
        )
        return int(row["n"]) if row else 0
    except Exception:
        logger.debug("[download_log] count_since failed", exc_info=True)
        return 0


def bytes_since(owner: str, since: float) -> int:
    try:
        row = (
            db.connection()
            .execute(
                "SELECT COALESCE(SUM(bytes), 0) AS n FROM downloads "
                "WHERE owner = ? AND success = 1 AND downloaded_at >= ?",
                (owner, since),
            )
            .fetchone()
        )
        return int(row["n"]) if row else 0
    except Exception:
        logger.debug("[download_log] bytes_since failed", exc_info=True)
        return 0


def has_isrc(isrc: str, *, owner: str | None = None) -> bool:
    """Whether a track with this ISRC was ever downloaded successfully.

    `owner=None` asks about the whole instance; passing an owner narrows it
    to one account, which is what multi-user mode wants — one person's copy
    is not another person's copy.
    """
    if not isrc:
        return False
    sql = "SELECT 1 FROM downloads WHERE isrc = ? AND success = 1"
    params: list = [isrc.upper()]
    if owner is not None:
        sql += " AND owner = ?"
        params.append(owner)
    try:
        return db.connection().execute(sql + " LIMIT 1", params).fetchone() is not None
    except Exception:
        logger.debug("[download_log] has_isrc failed", exc_info=True)
        return False


def recent(limit: int = 100, *, owner: str | None = None) -> list[DownloadRecord]:
    sql = "SELECT * FROM downloads"
    params: list = []
    if owner is not None:
        sql += " WHERE owner = ?"
        params.append(owner)
    sql += " ORDER BY downloaded_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
        rows = db.connection().execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]
    except Exception:
        logger.debug("[download_log] recent failed", exc_info=True)
        return []


def totals(owner: str | None = None) -> dict:
    sql = (
        "SELECT COUNT(*) AS tracks, COALESCE(SUM(bytes), 0) AS bytes "
        "FROM downloads WHERE success = 1"
    )
    params: list = []
    if owner is not None:
        sql += " AND owner = ?"
        params.append(owner)
    try:
        row = db.connection().execute(sql, params).fetchone()
        return {"tracks": int(row["tracks"]), "bytes": int(row["bytes"])}
    except Exception:
        logger.debug("[download_log] totals failed", exc_info=True)
        return {"tracks": 0, "bytes": 0}

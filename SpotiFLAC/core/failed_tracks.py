"""core/failed_tracks.py — the tracks that still have to be downloaded.

A run's failures used to live in two places, and neither outlived it: the
downloader's own list (printed in the end-of-run summary, then dropped) and
the frontend's queue (emptied by a page reload, and never filled at all for a
download the browser did not start). A playlist of 1,800 tracks that ended
with 40 failures therefore left no record of which 40 — the only way to find
them again was to run the whole playlist.

This is that record: one row per track per account, written by a
post-download hook the moment a track fails and removed the moment it
downloads — in the same run or any later one, including when a later run
finds the file already on disk. What is left in the table is exactly what is
still missing, which is what the queue panel's "Retry" re-submits.

Each row keeps the track's full metadata, not only a link: a retry has to be
able to run after a restart, when the tracklist the failure came from is no
longer in memory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from . import db

logger = logging.getLogger(__name__)

#: A provider error can run to a traceback; the panel shows one line of it.
_MAX_ERROR_CHARS = 500


def track_key(metadata: Any) -> str:
    """What identifies one track across runs.

    The source's own id first (the same track fetched from the same playlist
    twice has the same id), then the ISRC, then title and artists — a CSV row
    can carry nothing more. Empty when there is nothing to go on at all.
    """
    for attr in ("id", "isrc"):
        value = str(getattr(metadata, attr, "") or "").strip()
        if value:
            return f"{attr}:{value}"
    title = str(getattr(metadata, "title", "") or "").strip().lower()
    if not title:
        return ""
    artists = str(getattr(metadata, "artists", "") or "").strip().lower()
    return f"name:{title}|{artists}"


def _serialise(metadata: Any) -> str:
    dump = getattr(metadata, "model_dump", None)
    if dump is None:
        return ""
    try:
        return db.dumps(dump(mode="json"))
    except Exception:
        logger.debug("[failed_tracks] could not serialise a track", exc_info=True)
        return ""


def record_failure(
    owner: str, metadata: Any, error: str = "", source_url: str = ""
) -> None:
    """Adds a failed track, or counts one more attempt at it. Never raises.

    The same contract as download_log.record(): this runs from a
    post-download hook, and bookkeeping must never turn into a failure of the
    download it is keeping books on.
    """
    key = track_key(metadata)
    track = _serialise(metadata)
    if not key or not track:
        return
    now = time.time()
    try:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO failed_tracks (
                    owner, track_key, track, source_url, error,
                    attempts, first_failed_at, last_failed_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(owner, track_key) DO UPDATE SET
                    track          = excluded.track,
                    source_url     = excluded.source_url,
                    error          = excluded.error,
                    attempts       = failed_tracks.attempts + 1,
                    last_failed_at = excluded.last_failed_at
                """,
                (
                    owner or "",
                    key,
                    track,
                    source_url or "",
                    (error or "")[:_MAX_ERROR_CHARS],
                    now,
                    now,
                ),
            )
    except Exception:
        logger.debug("[failed_tracks] could not record %s", key, exc_info=True)


def resolve(owner: str, metadata: Any) -> None:
    """Forgets a track that has now downloaded. Never raises."""
    key = track_key(metadata)
    if not key:
        return
    try:
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM failed_tracks WHERE owner = ? AND track_key = ?",
                (owner or "", key),
            )
    except Exception:
        logger.debug("[failed_tracks] could not resolve %s", key, exc_info=True)


def hook(owner: str = "", source_url: str = "") -> Callable[[Any, Any], None]:
    """A post-download hook that keeps the table in step with each run.

    A success resolves the track, and so does a skip — a skipped track is one
    that is already on disk, which is the outcome a retry is after. Anything
    else is recorded, with `source_url` kept so the retry can rebuild the
    track's link (see core.tracklist.track_url).
    """

    def _on_track(result: Any, metadata: Any) -> None:
        if getattr(result, "success", False):
            resolve(owner, metadata)
        else:
            record_failure(
                owner, metadata, getattr(result, "error", "") or "", source_url
            )

    _on_track.__qualname__ = "failed_tracks.hook.on_track"
    return _on_track


def _rows(owner: str, keys: Iterable[str] | None) -> list[Any]:
    rows = (
        db.connection()
        .execute(
            "SELECT * FROM failed_tracks WHERE owner = ? ORDER BY last_failed_at DESC",
            (owner or "",),
        )
        .fetchall()
    )
    if keys is None:
        return rows
    # Filtered here rather than with an IN (...) built for the occasion: the
    # list is one account's failures, small by construction.
    wanted = {str(k) for k in keys}
    return [row for row in rows if row["track_key"] in wanted]


def list_for(owner: str = "") -> list[dict]:
    """What the queue panel shows, newest failure first. Never raises."""
    try:
        rows = _rows(owner, None)
    except Exception:
        logger.debug("[failed_tracks] could not read the list", exc_info=True)
        return []
    items: list[dict] = []
    for row in rows:
        track = db.loads(row["track"], {}) or {}
        items.append(
            {
                "key": row["track_key"],
                "title": track.get("title", ""),
                "artists": track.get("artists", ""),
                "album": track.get("album", ""),
                "error": row["error"],
                "attempts": int(row["attempts"]),
                "first_failed_at": float(row["first_failed_at"]),
                "last_failed_at": float(row["last_failed_at"]),
            }
        )
    return items


def retry_groups(
    owner: str = "", keys: Iterable[str] | None = None
) -> list[tuple[str, list[dict]]]:
    """The failures to re-submit, grouped by the collection they came from.

    Grouped because a bare track id only turns into a link next to the
    service that issued it, and one download job carries one source URL.
    Never raises.
    """
    try:
        rows = _rows(owner, keys)
    except Exception:
        logger.debug("[failed_tracks] could not read the list", exc_info=True)
        return []
    groups: dict[str, list[dict]] = {}
    for row in rows:
        track = db.loads(row["track"], None)
        if isinstance(track, dict):
            groups.setdefault(row["source_url"] or "", []).append(track)
    return list(groups.items())


def clear(owner: str = "", keys: Iterable[str] | None = None) -> int:
    """Drops failures from the list without retrying them.

    All of the account's, or only `keys`. Returns how many rows went. Never
    raises.
    """
    removed = 0
    try:
        with db.transaction() as conn:
            if keys is None:
                removed = conn.execute(
                    "DELETE FROM failed_tracks WHERE owner = ?", (owner or "",)
                ).rowcount
            else:
                for key in {str(k) for k in keys}:
                    removed += conn.execute(
                        "DELETE FROM failed_tracks WHERE owner = ? AND track_key = ?",
                        (owner or "", key),
                    ).rowcount
    except Exception:
        logger.debug("[failed_tracks] could not clear the list", exc_info=True)
        return 0
    return removed

"""Recent-fetches history — what was looked up, most recent first.

Backed by `core/db.py` rather than by rewriting `recent-fetches.json` on
every add. The file was capped at 50 entries not because 50 is a useful
number but because the whole list was re-serialised each time a track was
fetched; a table has no such reason to forget, so the cap is now an
order-of-magnitude larger and exists only to keep the GUI's "recent" list
from growing without end.

The legacy JSON file is imported exactly once — the fact that it has been
is recorded in the database, not inferred from the table being empty — and
then left alone (`cache_admin` still knows how to prune it). Nothing is lost
on upgrade and nothing has to be migrated by hand.

Note this is *lookup* history, not a record of downloads — see
`core/download_log.py` for that, which is a different question with a
different lifetime.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import db
from .models import TrackMetadata

logger = logging.getLogger(__name__)

#: Entries kept. Was 50, when every add rewrote the whole file.
MAX_ENTRIES = 500

LEGACY_FILE = Path.home() / ".cache" / "spotiflac" / "recent-fetches.json"

#: Set once the legacy JSON file has been folded in (or explicitly discarded
#: by a clear()). Durable rather than per-instance: the table being empty is
#: not evidence that the import has not happened — it is also what a user who
#: just cleared their history sees, and re-importing there would resurrect
#: every entry they had deleted.
_LEGACY_IMPORTED_KEY = "history_legacy_imported"


class HistoryManager:
    """Manages the search history (recent-fetches)."""

    def __init__(self, legacy_path: Path | None = None) -> None:
        # Kept as `.path` under its original name: callers (and tests) set it
        # to point the one-time legacy import somewhere else.
        self.path = legacy_path or LEGACY_FILE
        self._imported = False

    # ── Legacy import ─────────────────────────────────────────────────────

    def _import_legacy_once(self) -> None:
        """Folds a pre-SQLite `recent-fetches.json` into the table.

        Runs once per database, tracked by a `meta` row rather than by
        whether the table happens to be empty: empty is also what someone who
        just cleared their history sees, and re-importing there would bring
        back every entry they had deleted.
        """
        if self._imported:
            return
        self._imported = True

        if db.get_meta(_LEGACY_IMPORTED_KEY):
            return

        try:
            if not self.path.exists():
                return
            legacy = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt or unreadable legacy file is not worth a warning: the
            # history is a convenience, and starting empty is the right
            # outcome. (This is also what the old get_all() did.)
            return

        finally:
            # Whatever happened above — imported, empty, missing, corrupt —
            # this file has now had its one chance. Marking it here rather
            # than only on success means a malformed file is not re-parsed on
            # every single start.
            db.set_meta(_LEGACY_IMPORTED_KEY)

        if not isinstance(legacy, list):
            return

        rows = []
        for entry in legacy:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            rows.append(
                (
                    str(entry["id"]),
                    db.dumps(entry),
                    float(entry.get("fetched_at") or 0.0),
                )
            )
        if not rows:
            return
        try:
            with db.transaction() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO fetch_history "
                    "(track_id, payload, fetched_at) VALUES (?, ?, ?)",
                    rows,
                )
            logger.info("[history] Imported %d entries from %s", len(rows), self.path)
        except Exception:
            logger.debug("[history] legacy import failed", exc_info=True)

    # ── Public API (unchanged shape) ──────────────────────────────────────

    def add(self, metadata: TrackMetadata) -> None:
        self._import_legacy_once()
        entry = metadata.model_dump()
        stamp = time.time()
        # The payload field stays an integer, which is the shape every
        # existing consumer (the GUI's recent list, the legacy file) already
        # reads. The *column* keeps full precision, because it is what
        # ordering uses: at one-second resolution several tracks fetched in
        # the same second tie, and re-fetching a track would not move it back
        # to the top — ON CONFLICT keeps the original rowid, so the rowid
        # tiebreaker resolved the wrong way.
        entry["fetched_at"] = int(stamp)
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO fetch_history (track_id, payload, fetched_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(track_id) DO UPDATE SET "
                    "payload = excluded.payload, fetched_at = excluded.fetched_at",
                    (str(metadata.id), db.dumps(entry), stamp),
                )
                # Trim in the same transaction, so the table can never be seen
                # over its cap by a concurrent reader.
                conn.execute(
                    "DELETE FROM fetch_history WHERE track_id NOT IN ("
                    "  SELECT track_id FROM fetch_history "
                    "  ORDER BY fetched_at DESC, rowid DESC LIMIT ?"
                    ")",
                    (MAX_ENTRIES,),
                )
        except Exception:
            logger.debug("[history] could not add %s", metadata.id, exc_info=True)

    def get_all(self) -> list[dict]:
        self._import_legacy_once()
        try:
            rows = (
                db.connection()
                .execute(
                    "SELECT payload FROM fetch_history "
                    "ORDER BY fetched_at DESC, rowid DESC LIMIT ?",
                    (MAX_ENTRIES,),
                )
                .fetchall()
            )
        except Exception:
            logger.debug("[history] could not read history", exc_info=True)
            return []
        return [entry for entry in (db.loads(r["payload"]) for r in rows) if entry]

    def remove(self, track_id: str) -> bool:
        self._import_legacy_once()
        try:
            with db.transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM fetch_history WHERE track_id = ?", (str(track_id),)
                )
                return cursor.rowcount > 0
        except Exception:
            logger.debug("[history] could not remove %s", track_id, exc_info=True)
            return False

    def clear(self) -> None:
        # Marked as imported first: clearing and then having the legacy file
        # re-imported on the next read would be a bug with a very confusing
        # symptom.
        self._imported = True
        db.set_meta(_LEGACY_IMPORTED_KEY)
        try:
            with db.transaction() as conn:
                conn.execute("DELETE FROM fetch_history")
        except Exception:
            logger.debug("[history] could not clear history", exc_info=True)


def get_recent_fetches() -> list[dict]:
    return HistoryManager().get_all()


def clear_recent_fetches() -> None:
    HistoryManager().clear()

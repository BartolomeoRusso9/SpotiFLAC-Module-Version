"""core/db.py — the one durable store, for the state that must survive a restart.

Why this exists
---------------
Everything SpotiFLAC remembers has so far been a JSON file rewritten whole:
`recent-fetches.json` (capped at 50 entries), `isrc-cache.json`,
`provider_priority.json`, `profiles.json`. That is the right shape for
configuration — small, hand-editable, read once — and the wrong shape for
three things this module now backs:

  - **The job queue.** `JobQueue` kept every job in a dict in RAM. A restart
    of the container lost every queued download, which on the NAS/Docker
    deployment the queue exists to serve is precisely when it happens.
  - **The download log.** Nothing recorded what had actually been fetched, so
    "have I already got this?" could only be answered by looking at the
    filesystem, and per-account quotas had nothing to count.
  - **Subscriptions.** Following an artist means remembering which releases
    were already seen, forever, per artist — an append-mostly set that a
    rewrite-the-whole-file format handles badly.

None of the existing JSON files are migrated away. They keep working exactly
as they did; this sits alongside them for the state that is genuinely
relational and genuinely must not be lost.

Design notes
------------
`sqlite3` from the standard library, so nothing is added to `dependencies`.
WAL mode, because a `--web` instance has a request thread writing a job row
while a worker thread updates another — the default rollback journal makes
those block each other for the length of a write transaction.

Connections are per-thread (`threading.local`): a `sqlite3.Connection` is not
safe to share across threads, and the alternative — one connection behind a
lock — would serialise readers against the download workers for no benefit.

Every schema change is a new entry in `_MIGRATIONS`, applied in order inside
one transaction and recorded in `schema_version`. Never edit an existing
entry: an installed instance has already run it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Overridable so tests (and anyone running two instances on one machine)
#: don't share a database. ":memory:" works too, but note that each thread
#: then gets its *own* empty database — useful only for single-threaded tests.
DB_PATH_ENV = "SPOTIFLAC_DB_PATH"

DEFAULT_DB_PATH = Path.home() / ".spotiflac" / "spotiflac.db"

_local = threading.local()
_init_lock = threading.Lock()
#: Paths whose schema has already been brought up to date in this process.
#: Migration is idempotent, but doing it on every connection in every thread
#: would mean a write transaction per thread per run for nothing.
_migrated: set[str] = set()


def db_path() -> Path:
    override = os.getenv(DB_PATH_ENV)
    if override:
        return Path(override)
    return DEFAULT_DB_PATH


class DatabaseError(RuntimeError):
    """The store could not be opened or written."""


# ─────────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────────

_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # v1 — jobs, downloads, subscriptions, fetch history.
    (
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id          TEXT PRIMARY KEY,
            owner       TEXT NOT NULL DEFAULT '',
            payload     TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'queued',
            created_at  REAL NOT NULL DEFAULT 0,
            started_at  REAL,
            finished_at REAL,
            error       TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at)",
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            owner         TEXT NOT NULL DEFAULT '',
            spotify_id    TEXT NOT NULL DEFAULT '',
            isrc          TEXT NOT NULL DEFAULT '',
            title         TEXT NOT NULL DEFAULT '',
            artist        TEXT NOT NULL DEFAULT '',
            album         TEXT NOT NULL DEFAULT '',
            provider      TEXT NOT NULL DEFAULT '',
            file_path     TEXT NOT NULL DEFAULT '',
            format        TEXT NOT NULL DEFAULT '',
            bytes         INTEGER NOT NULL DEFAULT 0,
            success       INTEGER NOT NULL DEFAULT 1,
            downloaded_at REAL NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_downloads_owner ON downloads(owner, downloaded_at)",
        "CREATE INDEX IF NOT EXISTS idx_downloads_isrc ON downloads(isrc)",
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id              TEXT PRIMARY KEY,
            kind            TEXT NOT NULL DEFAULT 'artist',
            url             TEXT NOT NULL,
            name            TEXT NOT NULL DEFAULT '',
            output_dir      TEXT NOT NULL DEFAULT '',
            owner           TEXT NOT NULL DEFAULT '',
            include_groups  TEXT NOT NULL DEFAULT 'album,single',
            enabled         INTEGER NOT NULL DEFAULT 1,
            created_at      REAL NOT NULL DEFAULT 0,
            last_checked_at REAL,
            last_error      TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS subscription_seen (
            subscription_id TEXT NOT NULL,
            release_id      TEXT NOT NULL,
            title           TEXT NOT NULL DEFAULT '',
            seen_at         REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (subscription_id, release_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS fetch_history (
            track_id   TEXT PRIMARY KEY,
            payload    TEXT NOT NULL DEFAULT '{}',
            fetched_at REAL NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_fetch_history_time ON fetch_history(fetched_at)",
        # Small key/value side table for facts that are neither configuration
        # nor a row of anything: "the legacy history file has been imported"
        # is the first, and it has to be durable — an instance flag would let
        # a cleared history be repopulated from the old JSON on next start.
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """,
    ),
    # v2 — what a download *was*, not only that it happened.
    #
    # The log answered "how many, and how big" because that is all quotas and
    # deduplication need. The dashboard (core/stats.py) asks a different kind
    # of question — which genres, which era, how many hours — and none of it
    # can be reconstructed afterwards: the metadata that knew is gone by the
    # time the file is on disk, and reading a hundred thousand files back is
    # not an answer.
    #
    # Rows written before this migration keep empty values, so the dashboard
    # reports what it knows and says how much of the history predates it,
    # rather than quietly presenting a partial picture as the whole one.
    (
        "ALTER TABLE downloads ADD COLUMN genre TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN release_year TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0",
        # The dashboard's own access pattern: every row in a time window,
        # for everyone or for one account.
        "CREATE INDEX IF NOT EXISTS idx_downloads_time ON downloads(downloaded_at)",
    ),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = int(row[0]) if row and row[0] is not None else 0

    for index, statements in enumerate(_MIGRATIONS, start=1):
        if index <= current:
            continue
        # One transaction per migration: a half-applied schema change is the
        # one state no amount of retrying recovers from.
        with conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (index,))
        logger.debug("[db] applied migration v%d", index)


def _new_connection(path: Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        # Long enough to ride out another thread's write transaction, short
        # enough that a genuinely stuck lock surfaces instead of hanging the
        # request forever.
        timeout=15.0,
        # Explicit transactions via `with conn:`. "DEFERRED" is what the
        # legacy `""` value means anyway, and unlike `""` it is what the
        # stdlib actually documents (and what typeshed accepts).
        isolation_level="DEFERRED",
    )
    conn.row_factory = sqlite3.Row
    # WAL lets the request thread read while a worker thread writes. It is a
    # property of the database file, not the connection, so setting it on
    # every connect is a no-op after the first.
    with_wal = str(path) != ":memory:"
    if with_wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connection() -> sqlite3.Connection:
    """This thread's connection, opening (and migrating) it on first use."""
    path = db_path()
    key = str(path)
    existing = getattr(_local, "conn", None)
    if existing is not None and getattr(_local, "path", None) == key:
        return existing

    if existing is not None:
        # The path changed under us — only really happens in tests, where a
        # fixture repoints DB_PATH_ENV between cases.
        try:
            existing.close()
        except sqlite3.Error:
            pass

    try:
        conn = _new_connection(path)
    except sqlite3.Error as exc:
        msg = f"Could not open the SpotiFLAC database at {path}: {exc}"
        raise DatabaseError(msg) from exc

    with _init_lock:
        if key not in _migrated:
            try:
                _apply_migrations(conn)
            except sqlite3.Error as exc:
                conn.close()
                msg = f"Could not initialise the SpotiFLAC database at {path}: {exc}"
                raise DatabaseError(msg) from exc
            _migrated.add(key)

    _local.conn = conn
    _local.path = key
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """A write transaction on this thread's connection.

    Commits on a clean exit, rolls back on an exception — `with conn:` does
    exactly that, and this only adds the connection lookup so callers don't
    each repeat it.
    """
    conn = connection()
    with conn:
        yield conn


def close_thread_connection() -> None:
    """Closes this thread's connection, if it has one. Mostly for tests."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
        _local.path = None


def reset_for_tests() -> None:
    """Forgets every cached connection and migration marker in this process.

    Only the calling thread's connection can actually be closed from here —
    another thread's `sqlite3.Connection` must not be touched from this one —
    so this clears the *migration* memo and leaves other threads to notice
    the path change themselves on their next `connection()` call.
    """
    close_thread_connection()
    with _init_lock:
        _migrated.clear()


# ─────────────────────────────────────────────────────────────
#  Small helpers shared by the modules that use the store
# ─────────────────────────────────────────────────────────────


def dumps(value: Any) -> str:
    """JSON for a TEXT column, never raising on something unserialisable.

    A payload that cannot be encoded is stored as an explicit marker rather
    than aborting the write: losing the detail of one job's arguments is a
    great deal better than losing the job.
    """
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"__unserializable__": True})


def loads(text: str | None, fallback: Any = None) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


def now() -> float:
    return time.time()


def get_meta(key: str, default: str | None = None) -> str | None:
    """Reads one `meta` row. Never raises."""
    try:
        row = (
            connection()
            .execute("SELECT value FROM meta WHERE key = ?", (key,))
            .fetchone()
        )
        return row["value"] if row is not None else default
    except Exception:
        logger.debug("[db] could not read meta %s", key, exc_info=True)
        return default


def set_meta(key: str, value: str = "1") -> None:
    """Writes one `meta` row. Never raises."""
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    except Exception:
        logger.debug("[db] could not write meta %s", key, exc_info=True)

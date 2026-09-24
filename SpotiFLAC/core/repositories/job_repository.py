from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Any, List


class JobRepository:
    """Minimal SQLite-backed repository for the architectural foundation.

    The real project already has a durable database in `SpotiFLAC/core/db.py`;
    this repository provides the same contract in a smaller, testable layer so
    the application services can depend on a repository abstraction rather than
    ad hoc `sqlite3.connect(...)` calls.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        configured_path = os.getenv("SPOTIFLAC_DB_PATH")
        self.db_path = str(
            db_path
            or configured_path
            or Path.home() / ".spotiflac" / "repository-jobs.db"
        )
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS application_jobs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    priority INTEGER NOT NULL DEFAULT 0,
                    total_items INTEGER NOT NULL DEFAULT 0,
                    completed_items INTEGER NOT NULL DEFAULT 0,
                    created_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT
                )
                """)
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(application_jobs)"
                ).fetchall()
            }
            for name, definition in (
                ("priority", "INTEGER NOT NULL DEFAULT 0"),
                ("total_items", "INTEGER NOT NULL DEFAULT 0"),
                ("completed_items", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "REAL"),
                ("started_at", "REAL"),
                ("finished_at", "REAL"),
                ("error", "TEXT"),
            ):
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE application_jobs ADD COLUMN {name} {definition}"
                    )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS application_job_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS application_job_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'QUEUED',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    provider TEXT,
                    error TEXT,
                    result TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """)

    def create(self, payload: dict) -> dict:
        job_id = (
            payload.get("id")
            or f"job-{abs(hash(json.dumps(payload, sort_keys=True, default=str)))}"
        )
        stored_payload = payload.get("payload", payload)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO application_jobs "
                "(id, source, status, payload, priority, total_items, completed_items, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET source = excluded.source, "
                "status = excluded.status, payload = excluded.payload, "
                "priority = excluded.priority, total_items = excluded.total_items, "
                "completed_items = excluded.completed_items",
                (
                    job_id,
                    payload.get("source", ""),
                    payload.get("status", "QUEUED"),
                    json.dumps(stored_payload, default=str),
                    payload.get("priority", 0),
                    payload.get("total_items", 0),
                    payload.get("completed_items", 0),
                    payload.get("created_at", time.time()),
                ),
            )
        return {"id": job_id, **payload}

    def get(self, job_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, source, status, payload, priority, total_items, completed_items, "
                "created_at, started_at, finished_at, error "
                "FROM application_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        payload = json.loads(row["payload"])
        payload.update(
            {
                "id": row["id"],
                "source": row["source"],
                "status": row["status"],
                "priority": row["priority"],
                "total_items": row["total_items"],
                "completed_items": row["completed_items"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": row["error"],
            }
        )
        return payload

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, source, status, payload, priority, total_items, completed_items, "
                "created_at, started_at, finished_at, error "
                "FROM application_jobs ORDER BY rowid"
            ).fetchall()
        jobs = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload.update(
                {
                    "id": row["id"],
                    "source": row["source"],
                    "status": row["status"],
                    "priority": row["priority"],
                    "total_items": row["total_items"],
                    "completed_items": row["completed_items"],
                    "created_at": row["created_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "error": row["error"],
                }
            )
            jobs.append(payload)
        return jobs

    def update_status(self, job_id: str, status: str) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_jobs SET status = ?, "
                "started_at = CASE WHEN ? = 'RUNNING' THEN ? ELSE started_at END, "
                "finished_at = CASE "
                "WHEN ? IN ('QUEUED', 'RETRYING', 'RUNNING', 'PAUSED') THEN NULL "
                "WHEN ? IN ('DONE', 'FAILED', 'CANCELLED') THEN ? "
                "ELSE finished_at END WHERE id = ?",
                (status, status, now, status, status, now, job_id),
            )
        return self.get(job_id)

    def update_progress(self, job_id: str, completed_items: int) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_jobs SET completed_items = ? WHERE id = ?",
                (completed_items, job_id),
            )
        return self.get(job_id)

    def update_error(self, job_id: str, error: str | None) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_jobs SET error = ? WHERE id = ?",
                (error, job_id),
            )
        return self.get(job_id)

    def create_attempt(self, job_id: str, status: str = "RUNNING") -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO application_job_attempts "
                "(job_id, status, started_at) VALUES (?, ?, ?)",
                (job_id, status, time.time()),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("failed to create job attempt")
            return cursor.lastrowid

    def finish_attempt(
        self, attempt_id: int, status: str, error: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_job_attempts SET status = ?, finished_at = ?, error = ? "
                "WHERE id = ?",
                (status, time.time(), error, attempt_id),
            )

    def list_attempts(self, job_id: str) -> List[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, job_id, status, started_at, finished_at, error "
                "FROM application_job_attempts WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_items(self, job_id: str, sources: List[str]) -> List[dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO application_job_items "
                "(job_id, source, created_at, updated_at) VALUES (?, ?, ?, ?)",
                [(job_id, source, now, now) for source in sources],
            )
        return self.list_items(job_id)

    def list_items(self, job_id: str) -> List[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, job_id, source, status, attempts, provider, error, result, "
                "created_at, updated_at FROM application_job_items "
                "WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_item(self, item_id: int, **changes: object) -> dict[str, Any]:
        allowed = {"status", "attempts", "provider", "error", "result"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            raise ValueError("no item fields to update")
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE application_job_items SET {assignments} WHERE id = ?",
                (*values.values(), item_id),
            )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, job_id, source, status, attempts, provider, error, result, "
                "created_at, updated_at FROM application_job_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return dict(row)

    def reset_items_for_retry(self, job_id: str) -> List[dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_job_items SET status = 'QUEUED', error = NULL, "
                "provider = NULL, result = NULL, updated_at = ? "
                "WHERE job_id = ? AND status IN ('FAILED', 'CANCELLED', 'RUNNING')",
                (now, job_id),
            )
        return self.list_items(job_id)

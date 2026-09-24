from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


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
            db_path or configured_path or Path.home() / ".spotiflac" / "repository-jobs.db"
        )
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS application_jobs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}'
                )
                """
            )

    def create(self, payload: dict) -> dict:
        job_id = payload.get("id") or f"job-{abs(hash(json.dumps(payload, sort_keys=True, default=str)))}"
        stored_payload = payload.get("payload", payload)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO application_jobs (id, source, status, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET source = excluded.source, status = excluded.status, payload = excluded.payload",
                (
                    job_id,
                    payload.get("source", ""),
                    payload.get("status", "QUEUED"),
                    json.dumps(stored_payload, default=str),
                ),
            )
        return {"id": job_id, **payload}

    def get(self, job_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, source, status, payload FROM application_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        payload = json.loads(row["payload"])
        payload.update({"id": row["id"], "source": row["source"], "status": row["status"]})
        return payload

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, source, status, payload FROM application_jobs ORDER BY rowid"
            ).fetchall()
        jobs = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload.update({"id": row["id"], "source": row["source"], "status": row["status"]})
            jobs.append(payload)
        return jobs

    def update_status(self, job_id: str, status: str) -> dict:
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_jobs SET status = ? WHERE id = ?",
                (status, job_id),
            )
        return self.get(job_id)

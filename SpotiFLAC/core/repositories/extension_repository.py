from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class ExtensionRepository:
    """Minimal SQLite-backed extension state repository for the architecture foundation."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or Path(".spotiflac/repository-extensions.db"))
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
                CREATE TABLE IF NOT EXISTS extensions (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL DEFAULT '{}'
                )
                """
            )

    def upsert(self, payload: dict) -> dict:
        extension_id = payload["id"]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO extensions (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (extension_id, json.dumps(payload)),
            )
        return self.get(extension_id)

    def get(self, extension_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM extensions WHERE id = ?",
                (extension_id,),
            ).fetchone()
        if row is None:
            raise KeyError(extension_id)
        return json.loads(row["payload"])

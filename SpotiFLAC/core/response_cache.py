"""Persistent TTL cache for non-sensitive server responses."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_CACHE_ROOT = Path(
    os.getenv("SPOTIFLAC_CACHE_DIR", str(Path.home() / ".cache" / "spotiflac")),
) / "responses"


def _path(namespace: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    safe_namespace = "".join(c if c.isalnum() or c in "-_" else "_" for c in namespace)
    return _CACHE_ROOT / safe_namespace / f"{digest}.json"


def get(namespace: str, key: str, ttl: float) -> Any | None:
    path = _path(namespace, key)
    try:
        if not path.exists() or time.time() - path.stat().st_mtime > ttl:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def put(namespace: str, key: str, value: Any) -> None:
    path = _path(namespace, key)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".response-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

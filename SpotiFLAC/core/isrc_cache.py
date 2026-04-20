# SpotiFLAC/core/isrc_cache.py
"""
Cache persistente per ISRC — port di isrc_cache.go.
Evita chiamate ridondanti a Songlink/Soundplate per ISRC già risolti.
"""
from __future__ import annotations
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_FILE = Path.home() / ".cache" / "spotiflac" / "isrc-cache.json"
_cache_lock = threading.Lock()
_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _CACHE_FILE.exists():
            _cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        else:
            _cache = {}
    except Exception as exc:
        logger.warning("[isrc_cache] Load failed: %s", exc)
        _cache = {}
    return _cache


def _save(cache: dict) -> None:
    try:
        _CACHE_FILE.write_text(
            json.dumps(cache, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("[isrc_cache] Save failed: %s", exc)


def get_cached_isrc(track_id: str) -> str:
    """Ritorna ISRC cached o stringa vuota."""
    track_id = track_id.strip()
    if not track_id:
        return ""
    with _cache_lock:
        cache = _load()
        entry = cache.get(track_id, {})
        return entry.get("isrc", "").upper().strip()


def put_cached_isrc(track_id: str, isrc: str) -> None:
    """Salva ISRC in cache."""
    track_id = track_id.strip()
    isrc     = isrc.upper().strip()
    if not track_id or not isrc:
        return
    with _cache_lock:
        cache = _load()
        cache[track_id] = {"isrc": isrc, "updated_at": int(time.time())}
        _save(cache)
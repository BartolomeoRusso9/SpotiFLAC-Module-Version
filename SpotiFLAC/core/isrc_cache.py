# backend/core/isrc_cache.py
"""
Persistent ISRC cache — port of isrc_cache.go.
Avoids redundant Songlink/Soundplate calls for already-resolved ISRCs.
Async version with aiofiles for non-blocking I/O.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path

try:
    import aiofiles
except ImportError:
    aiofiles = None

logger = logging.getLogger(__name__)

_CACHE_FILE = Path.home() / ".cache" / "spotiflac" / "isrc-cache.json"
_cache_lock: asyncio.Lock | None = None
_cache: dict[str, dict] | None = None


async def _get_lock() -> asyncio.Lock:
    """Get or create the asyncio.Lock for cache access."""
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


async def _load_async() -> dict[str, dict]:
    """Async load cache from disk using aiofiles (falls back to sync if unavailable)."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

        if aiofiles:
            if _CACHE_FILE.exists():
                async with aiofiles.open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                    _cache = json.loads(content)
            else:
                _cache = {}
        else:
            # Fallback to sync if aiofiles not available
            if _CACHE_FILE.exists():
                _cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            else:
                _cache = {}
    except Exception as exc:
        logger.warning("[isrc_cache] Load failed: %s", exc)
        _cache = {}
    return _cache


async def _save_async(cache: dict) -> None:
    """Async save cache to disk using aiofiles (falls back to sync if unavailable)."""
    try:
        cache_json = json.dumps(cache, indent=2)
        if aiofiles:
            async with aiofiles.open(_CACHE_FILE, "w", encoding="utf-8") as f:
                await f.write(cache_json)
        else:
            # Fallback to sync if aiofiles not available
            _CACHE_FILE.write_text(cache_json, encoding="utf-8")
    except Exception as exc:
        logger.warning("[isrc_cache] Save failed: %s", exc)


async def get_cached_isrc_async(track_id: str) -> str:
    """Async: Returns ISRC cached or empty string."""
    track_id = track_id.strip()
    if not track_id:
        return ""
    lock = await _get_lock()
    async with lock:
        cache = await _load_async()
        entry = cache.get(track_id, {})
        return entry.get("isrc", "").upper().strip()


async def put_cached_isrc_async(track_id: str, isrc: str) -> None:
    """Async: Save ISRC to cache."""
    track_id = track_id.strip()
    isrc = isrc.upper().strip()
    if not track_id or not isrc:
        return
    lock = await _get_lock()
    async with lock:
        cache = await _load_async()
        cache[track_id] = {"isrc": isrc, "updated_at": int(time.time())}
        await _save_async(cache)


# Legacy sync wrappers for backward compatibility (if needed)
def get_cached_isrc(track_id: str) -> str:
    """Sync wrapper (deprecated, use get_cached_isrc_async instead)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, fallback to thread pool
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            return executor.submit(
                asyncio.run, get_cached_isrc_async(track_id)
            ).result()
    else:
        # Inside async context, can't use asyncio.run()
        raise RuntimeError(
            "get_cached_isrc() called from async context. "
            "Use get_cached_isrc_async() instead."
        )


def put_cached_isrc(track_id: str, isrc: str) -> None:
    """Sync wrapper (deprecated, use put_cached_isrc_async instead)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, fallback to thread pool
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(asyncio.run, put_cached_isrc_async(track_id, isrc)).result()
    else:
        # Inside async context, can't use asyncio.run()
        raise RuntimeError(
            "put_cached_isrc() called from async context. "
            "Use put_cached_isrc_async() instead."
        )

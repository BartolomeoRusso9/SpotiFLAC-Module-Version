from __future__ import annotations

import json
import time
from pathlib import Path
import asyncio
import logging

logger = logging.getLogger(__name__)

_io_lock = asyncio.Lock()
_SESSION_FILE = Path.home() / ".cache" / "spotiflac" / "session.json"
_MAX_HISTORY  = 20


def _read_file_sync() -> dict:
    """Helper sincrono eseguito nei thread pool per la lettura su disco."""
    if _SESSION_FILE.exists():
        return json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    return {"last_folder": "", "url_history": []}


def _write_file_sync(data: dict) -> None:
    """Helper sincrono eseguito nei thread pool per la scrittura su disco."""
    _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _load_async() -> dict:
    async with _io_lock:
        try:
            # Deleghiamo lettura disco e parsing JSON a un thread lavoratore
            return await asyncio.to_thread(_read_file_sync)
        except Exception as exc:
            logger.debug("[session] Read error: %s", exc)
    return {"last_folder": "", "url_history": []}


async def _save_async(data: dict) -> None:
    async with _io_lock:
        try:
            # Deleghiamo scrittura disco e dump JSON a un thread lavoratore
            await asyncio.to_thread(_write_file_sync, data)
        except Exception as exc:
            logger.debug("[session] Write error: %s", exc)

# ---------------------------------------------------------------------------
# Output folder
# ---------------------------------------------------------------------------

async def get_last_folder_async() -> str:
    """Returns l'ultima cartella di output usata, o stringa vuota."""
    data = await _load_async()
    return data.get("last_folder", "")


async def set_last_folder_async(folder: str) -> None:
    """Memorizza l'ultima cartella di output utilizzata."""
    if not folder:
        return
    data = await _load_async()
    data["last_folder"] = folder
    await _save_async(data)


# ---------------------------------------------------------------------------
# URL history
# ---------------------------------------------------------------------------

async def get_url_history_async() -> list[dict]:
    """
    Returns la cronologia URL in ordine dal più recente al meno recente.
    Ogni entry è: {"url": str, "label": str, "cover": str, "track_count": int,
                   "url_type": str, "artist": str, "at": int (unix timestamp)}
    """
    data = await _load_async()
    return data.get("url_history", [])


def _normalize_history_url(url: str) -> str:
    """
    Normalizza le URL salvate nella history (operazione veloce string based, resta sync).
    - spotify:track:ID -> https://open.spotify.com/track/ID
    - open.spotify.com/... -> https://open.spotify.com/...
    - mantiene http(s) invariati
    """
    if not url:
        return ""
    s = str(url).strip()
    try:
        if s.startswith('spotify:'):
            parts = s.split(':')
            if len(parts) >= 3:
                typ = parts[1]
                id_part = ':'.join(parts[2:])
                return f"https://open.spotify.com/{typ}/{id_part}"
        if s.startswith('http://') or s.startswith('https://'):
            return s
        if s.startswith('open.spotify.com') or s.startswith('play.spotify.com'):
            return f"https://{s}"
        return s
    except Exception:
        return s


async def add_url_to_history_async(url: str, label: str = "", cover: str = "", track_count: int = 0, url_type: str = "", artist: str = "") -> None:
    """
    Aggiunge un URL alla cronologia (o lo sposta in cima se già presente).
    """
    if not url:
        return
    nurl = _normalize_history_url(url)
    data = await _load_async()
    
    # Rimuovi eventuali occorrenze della stessa URL normalizzata
    history = [h for h in data.get("url_history", []) if h.get("url") != nurl]
    history.insert(0, {
        "url":         nurl,
        "label":       label or nurl[:65],
        "cover":       cover or "",
        "track_count": track_count,
        "url_type":    url_type,
        "artist":      artist,
        "at":          int(time.time()),
    })
    
    data["url_history"] = history[:_MAX_HISTORY]
    await _save_async(data)


async def clear_url_history_async() -> None:
    """Svuota completamente la cronologia degli URL."""
    data = await _load_async()
    data["url_history"] = []
    await _save_async(data)


async def remove_url_from_history_async(url: str) -> None:
    """Removes un singolo URL dalla cronologia."""
    if not url:
        return
    nurl = _normalize_history_url(url)
    data = await _load_async()
    
    history = [h for h in data.get("url_history", []) if h.get("url") != nurl]
    data["url_history"] = history
    await _save_async(data)
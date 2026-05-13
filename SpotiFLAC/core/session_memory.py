"""
Session memory — ricorda l'ultima cartella di output e la cronologia degli URL.
File: ~/.cache/spotiflac/session.json

Integra HistoryManager esistente (core/history.py) aggiungendo:
  - last_folder: ultima directory di output usata
  - url_history: cronologia degli URL inseriti dall'utente (max 20)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_SESSION_FILE = Path.home() / ".cache" / "spotiflac" / "session.json"
_MAX_HISTORY  = 20


def _load() -> dict:
    try:
        if _SESSION_FILE.exists():
            return json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"last_folder": "", "url_history": []}


def _save(data: dict) -> None:
    try:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Output folder
# ---------------------------------------------------------------------------

def get_last_folder() -> str:
    """Restituisce l'ultima cartella di output usata, o stringa vuota."""
    return _load().get("last_folder", "")


def set_last_folder(folder: str) -> None:
    """Memorizza l'ultima cartella di output utilizzata."""
    if not folder:
        return
    data = _load()
    data["last_folder"] = folder
    _save(data)


# ---------------------------------------------------------------------------
# URL history
# ---------------------------------------------------------------------------

def get_url_history() -> list[dict]:
    """
    Ritorna la cronologia URL in ordine dal più recente al meno recente.
    Ogni entry è: {"url": str, "label": str, "at": int (unix timestamp)}
    """
    return _load().get("url_history", [])


def add_url_to_history(url: str, label: str = "") -> None:
    """
    Aggiunge un URL alla cronologia (o lo sposta in cima se già presente).
    Il label è una descrizione breve opzionale (es. nome della collection).
    """
    if not url:
        return
    data    = _load()
    history = [h for h in data.get("url_history", []) if h.get("url") != url]
    history.insert(0, {
        "url":   url,
        "label": label or url[:65],
        "at":    int(time.time()),
    })
    data["url_history"] = history[:_MAX_HISTORY]
    _save(data)


def clear_url_history() -> None:
    """Svuota completamente la cronologia degli URL."""
    data = _load()
    data["url_history"] = []
    _save(data)


def remove_url_from_history(url: str) -> None:
    """Rimuove un singolo URL dalla cronologia."""
    data    = _load()
    history = [h for h in data.get("url_history", []) if h.get("url") != url]
    data["url_history"] = history
    _save(data)
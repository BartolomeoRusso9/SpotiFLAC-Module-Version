"""
Profile management — salva/carica preset di configurazione con nome.
File: ~/.cache/spotiflac/profiles.json

Uso:
    save_profile("tidal-hires", cfg)
    cfg = get_profile("tidal-hires")
    names = list_profiles()
"""
from __future__ import annotations

import json
import time
from pathlib import Path
import threading
import logging

logger = logging.getLogger(__name__)
_io_lock = threading.Lock()
_PROFILES_FILE = Path.home() / ".cache" / "spotiflac" / "profiles.json"

# Chiavi che vengono salvate in un profilo (esclude URL, cartella, token personali)
_PROFILE_KEYS = [
    "services", "quality", "filename_format",
    "use_track_numbers", "use_album_track_numbers",
    "use_artist_subfolders", "use_album_subfolders",
    "first_artist_only", "allow_fallback",
    "embed_lyrics", "lyrics_providers",
    "enrich_metadata", "enrich_providers",
    "track_max_retries", "post_download_action", "post_download_command",
    "include_featuring",
]


def _load() -> dict:
    with _io_lock:
        try:
            if _PROFILES_FILE.exists():
                return json.loads(_PROFILES_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("[profile] Read error: %s", exc)
    return {}


def _write(profiles: dict) -> None:
    with _io_lock:
        try:
             _PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
             _PROFILES_FILE.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("[profile] Write error: %s", exc)



def list_profiles() -> list[str]:
    """Restituisce i nomi di tutti i profili salvati, in ordine alfabetico."""
    return sorted(_load().keys())


def get_profile(name: str) -> dict | None:
    """
    Carica un profilo per nome.
    Ritorna None se il profilo non esiste.
    """
    return _load().get(name)


def save_profile(name: str, cfg: dict) -> None:
    """
    Salva le chiavi rilevanti di cfg come profilo nominato.
    Sovrascrive eventuali profili preesistenti con lo stesso nome.
    """
    profiles = _load()
    profiles[name] = {k: cfg[k] for k in _PROFILE_KEYS if k in cfg}
    profiles[name]["_saved_at"] = int(time.time())
    _write(profiles)


def delete_profile(name: str) -> bool:
    """
    Elimina un profilo per nome.
    Ritorna True se il profilo esisteva, False altrimenti.
    """
    profiles = _load()
    if name not in profiles:
        return False
    del profiles[name]
    _write(profiles)
    return True


def rename_profile(old_name: str, new_name: str) -> bool:
    """Rinomina un profilo. Ritorna True se l'operazione riesce."""
    profiles = _load()
    if old_name not in profiles or new_name in profiles:
        return False
    profiles[new_name] = profiles.pop(old_name)
    _write(profiles)
    return True
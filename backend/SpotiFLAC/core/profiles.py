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
# Chiavi di runtime o sensibili che NON devono essere salvate in un profilo
_EXCLUDE_FROM_PROFILE = ["url", "output_path", "qobuz_token"]


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
    Salva l'intera configurazione come profilo nominato, escludendo i dati di runtime.
    Sovrascrive eventuali profili preesistenti con lo stesso nome.
    """
    profiles = _load()
    # Salva tutto tranne url, output_path e le chiavi interne (che iniziano con _)
    profiles[name] = {k: v for k, v in cfg.items() if k not in _EXCLUDE_FROM_PROFILE and not k.startswith("_")}
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
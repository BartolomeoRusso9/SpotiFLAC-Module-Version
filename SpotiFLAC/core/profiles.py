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

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)
_io_lock = threading.Lock()
_PROFILES_FILE = Path.home() / ".cache" / "spotiflac" / "profiles.json"


class ProfileConfig(BaseModel):
    services: list[str] = Field(default_factory=lambda: ["tidal"])
    filename_format: str = "{title} - {artist}"
    use_track_numbers: bool = False
    use_album_track_numbers: bool = False
    use_artist_subfolders: bool = False
    use_album_subfolders: bool = False
    first_artist_only: bool = False
    allow_fallback: bool = True
    quality: str = "LOSSLESS"
    embed_lyrics: bool = True
    lyrics_providers: list[str] = Field(
        default_factory=lambda: ["spotify", "apple", "musixmatch", "amazon", "lrclib"]
    )
    enrich_metadata: bool = True
    enrich_providers: list[str] = Field(
        default_factory=lambda: ["deezer", "apple", "qobuz", "tidal", "soundcloud"]
    )
    track_max_retries: int = 0
    post_download_action: str = "none"
    post_download_command: str = ""
    qobuz_local_api_url: str | None = None
    tidal_custom_api: str | None = None
    timeout_s: int | None = None
    loop: int | None = None
    log_level: int | None = None
    output_path: str | None = None

    model_config = {"extra": "ignore"}

    @field_validator("log_level", mode="before")
    @classmethod
    def parse_log_level(cls, value: int | str | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            normalized = value.strip().upper()
            if not normalized:
                return None
            if normalized.isdigit():
                return int(normalized)
            standard_level = logging.getLevelName(normalized)
            if isinstance(standard_level, int):
                return standard_level
            aliases = {
                "WARN": "WARNING",
                "ERR": "ERROR",
                "CRIT": "CRITICAL",
                "FATAL": "CRITICAL",
            }
            mapped = aliases.get(normalized)
            if mapped:
                return logging.getLevelName(mapped)
            raise ValueError(f"Invalid log level: {value}")
        raise TypeError("log_level must be an integer or a named log level string")


def _load() -> dict:
    with _io_lock:
        try:
            if _PROFILES_FILE.exists():
                raw = json.loads(_PROFILES_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    validated: dict[str, dict] = {}
                    for name, profile in raw.items():
                        if not isinstance(profile, dict):
                            logger.debug("[profile] skipping invalid profile %s", name)
                            continue
                        try:
                            validated[name] = ProfileConfig.model_validate(profile).model_dump(exclude_none=True)
                        except ValidationError as exc:
                            logger.warning("[profile] invalid profile %s: %s", name, exc)
                    return validated
        except json.JSONDecodeError as exc:
            logger.warning("[profile] profiles.json is invalid JSON: %s", exc)
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
    """Returns i nomi di tutti i profili salvati, in ordine alfabetico."""
    return sorted(_load().keys())


def get_profile(name: str) -> dict | None:
    """
    Carica un profilo per nome.
    Returns None se il profilo non esiste.
    """
    return _load().get(name)


def save_profile(name: str, cfg: dict) -> None:
    """
    Salva l'intera configurazione come profilo nominato, escludendo i dati di runtime.
    Sovrascrive eventuali profili preesistenti con lo stesso nome.
    """
    profiles = _load()
    validated = ProfileConfig.model_validate(cfg)
    profile_data = validated.model_dump(exclude_none=True)
    for runtime_key in ("url", "output_path", "qobuz_token"):
        profile_data.pop(runtime_key, None)
    profile_data["_saved_at"] = int(time.time())
    profiles[name] = profile_data
    _write(profiles)


def delete_profile(name: str) -> bool:
    """
    Elimina un profilo per nome.
    Returns True se il profilo esisteva, False altrimenti.
    """
    profiles = _load()
    if name not in profiles:
        return False
    del profiles[name]
    _write(profiles)
    return True


def rename_profile(old_name: str, new_name: str) -> bool:
    """Rinomina un profilo. Returns True se l'operazione riesce."""
    profiles = _load()
    if old_name not in profiles or new_name in profiles:
        return False
    profiles[new_name] = profiles.pop(old_name)
    _write(profiles)
    return True
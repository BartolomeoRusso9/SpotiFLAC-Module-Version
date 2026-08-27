"""Profile management — save/load named configuration presets.
File: ~/.cache/spotiflac/profiles.json.

Async usage:
    await save_profile_async("tidal-hires", cfg)
    cfg = await get_profile_async("tidal-hires")
    names = await list_profiles_async()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

# Using asyncio.Lock instead of threading.Lock so it doesn't block the event loop
_io_lock = asyncio.Lock()
_PROFILES_FILE = Path.home() / ".cache" / "spotiflac" / "profiles.json"


class ProfileConfig(BaseModel):
    output_dir: str = "./Downloads"
    services: list[str] = Field(default_factory=lambda: ["tidal"])
    filename_format: str = "{title} - {artist}"
    use_track_numbers: bool = False
    use_album_track_numbers: bool = False
    use_artist_subfolders: bool = False
    use_album_subfolders: bool = False
    first_artist_only: bool = False
    artist_separator: str | None = None
    allow_fallback: bool = True
    quality: str = "LOSSLESS"
    embed_lyrics: bool = True
    lyrics_providers: list[str] = Field(
        default_factory=lambda: ["apple", "lrclib"],
    )
    enrich_metadata: bool = True
    enrich_providers: list[str] = Field(
        default_factory=lambda: ["deezer", "apple", "qobuz", "tidal"],
    )
    transcode_to: str | None = None
    transcode_bitrate: str = "320k"
    transcode_keep_original: bool = False
    m3u_format: str = "m3u8"
    track_max_retries: int = 0
    post_download_action: str = "none"
    post_download_command: str = ""
    qobuz_local_api_url: str | None = None
    tidal_custom_api: str | None = None
    timeout_s: int | None = None
    loop: int | None = None
    watch: int | None = None
    log_level: int | None = None
    output_path: str | None = None
    include_featuring: bool = True
    max_concurrent_downloads: int = 2

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
            msg = f"Invalid log level: {value}"
            raise ValueError(msg)
        msg = "log_level must be an integer or a named log level string"
        raise TypeError(msg)


# Synchronous I/O helpers to be executed in a thread
def _read_file_sync() -> str | None:
    if _PROFILES_FILE.exists():
        return _PROFILES_FILE.read_text(encoding="utf-8")
    return None


def _write_file_sync(data_str: str) -> None:
    _PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROFILES_FILE.write_text(data_str, encoding="utf-8")


async def _load_async() -> dict:
    async with _io_lock:
        try:
            raw_data = await asyncio.to_thread(_read_file_sync)
            if raw_data:
                raw = json.loads(raw_data)
                if isinstance(raw, dict):
                    validated: dict[str, dict] = {}
                    for name, profile in raw.items():
                        if not isinstance(profile, dict):
                            logger.debug("[profile] skipping invalid profile %s", name)
                            continue
                        try:
                            # Pydantic validation is extremely fast in RAM
                            validated[name] = ProfileConfig.model_validate(
                                profile,
                            ).model_dump(exclude_none=True)
                        except ValidationError as exc:
                            logger.warning(
                                "[profile] invalid profile %s: %s",
                                name,
                                exc,
                            )
                    return validated
        except json.JSONDecodeError as exc:
            logger.warning("[profile] profiles.json is invalid JSON: %s", exc)
        except Exception as exc:
            logger.debug("[profile] Read error: %s", exc)
    return {}


async def _write_async(profiles: dict) -> None:
    async with _io_lock:
        try:
            data_str = json.dumps(profiles, indent=2)
            await asyncio.to_thread(_write_file_sync, data_str)
        except Exception as exc:
            logger.debug("[profile] Write error: %s", exc)


async def list_profiles_async() -> list[str]:
    """Returns the names of all saved profiles, in alphabetical order."""
    profiles = await _load_async()
    return sorted(profiles.keys())


async def get_profile_async(name: str) -> dict | None:
    """Carica un profilo per nome.
    Returns None se il profilo non esiste.
    """
    profiles = await _load_async()
    return profiles.get(name)


async def save_profile_async(name: str, cfg: dict) -> None:
    """Saves the entire configuration as a named profile, excluding runtime data.
    Overwrites any pre-existing profile with the same name.
    """
    profiles = await _load_async()
    validated = ProfileConfig.model_validate(cfg)
    profile_data = validated.model_dump(exclude_none=True)
    for runtime_key in ("url", "output_path", "qobuz_token"):
        profile_data.pop(runtime_key, None)
    profile_data["_saved_at"] = int(time.time())
    profiles[name] = profile_data
    await _write_async(profiles)


async def delete_profile_async(name: str) -> bool:
    """Deletes a profile by name.
    Returns True if the profile existed, False otherwise.
    """
    profiles = await _load_async()
    if name not in profiles:
        return False
    del profiles[name]
    await _write_async(profiles)
    return True


async def rename_profile_async(old_name: str, new_name: str) -> bool:
    """Renames a profile. Returns True if the operation succeeds."""
    profiles = await _load_async()
    if old_name not in profiles or new_name in profiles:
        return False
    profiles[new_name] = profiles.pop(old_name)
    await _write_async(profiles)
    return True

"""Single source of truth for Mornye's on-disk locations.

State is stored under ``~/.mornye`` (override: ``$MORNYE_DATA_DIR``).

    ~/.mornye/               durable data — losing it loses something real
      mornye.db
      extensions/
      signed_sessions/
      web_users.json, trusted_keys.json, registry_settings.json, ...

    ~/.mornye/.cache/        regenerable state (override: $MORNYE_CACHE_DIR)
      endpoints_cache.txt, responses/, library-index/, session.json,
      provider_priority.json, isrc-cache.json, gui-settings.json, ...
"""

from __future__ import annotations

import os
from pathlib import Path

from .env import env_value


def default_download_dir() -> str:
    """Where downloads go unless told otherwise: ``~/Music/Mornye``.

    One definition, because every frontend offers it and they must agree —
    a GUI that defaults somewhere the terminal UI does not is two libraries
    on one machine. Returned as a string: it is handed straight to the
    downloader and shown in a text field, and neither wants a Path.

    ``$MORNYE_DOWNLOAD_DIR``, when set, replaces it: a container has no
    meaningful ``~/Music``, and the web GUI's folder picker cannot leave
    ``$HOME`` to choose a mounted library on its own.
    """
    override = (env_value("MORNYE_DOWNLOAD_DIR", "") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return str(Path.home() / "Music" / "Mornye")


def data_dir() -> Path:
    """The one directory everything Mornye writes lives under.

    ``$MORNYE_DATA_DIR``, when set, is used verbatim instead of
    ``~/.mornye`` — the way to keep the data somewhere that does not
    depend on ``$HOME`` (a container run as an arbitrary UID, a read-only
    root filesystem).
    """
    override = (env_value("MORNYE_DATA_DIR", "") or "").strip()
    if override:
        return Path(override).expanduser()

    return Path.home() / ".mornye"


def data_path(*parts: str) -> Path:
    return data_dir().joinpath(*parts)


def cache_dir() -> Path:
    """The subdirectory for disposable / regenerable state.

    ``$MORNYE_CACHE_DIR``, when set, is used verbatim as the cache
    directory (the seam the test-suite and packagers rely on). Otherwise it
    is ``~/.mornye/.cache``.
    """
    override = env_value("MORNYE_CACHE_DIR")
    return Path(override).expanduser() if override else data_dir() / ".cache"


def cache_path(*parts: str) -> Path:
    return cache_dir().joinpath(*parts)

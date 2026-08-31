"""Single source of truth for SpotiFLAC's on-disk locations.

Everything SpotiFLAC writes lives under one directory, ``~/.spotiflac``:

    ~/.spotiflac/               durable data — losing it loses something real
      spotiflac.db
      extensions/
      signed_sessions/
      web_users.json, trusted_keys.json, registry_settings.json, ...

    ~/.spotiflac/.cache/        regenerable state (override: $SPOTIFLAC_CACHE_DIR)
      endpoints_cache.txt, responses/, library-index/, session.json,
      provider_priority.json, isrc-cache.json, gui-settings.json, ...

The cache half used to live at ``~/.cache/spotiflac``. Nothing here migrates
the old directory — the caches simply rebuild themselves at the new path.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """The one directory everything SpotiFLAC writes lives under."""
    return Path.home() / ".spotiflac"


def data_path(*parts: str) -> Path:
    return data_dir().joinpath(*parts)


def cache_dir() -> Path:
    """The subdirectory for disposable / regenerable state.

    ``$SPOTIFLAC_CACHE_DIR``, when set, is used verbatim as the cache
    directory (the seam the test-suite and packagers rely on). Otherwise it
    is ``~/.spotiflac/.cache``.
    """
    override = os.getenv("SPOTIFLAC_CACHE_DIR")
    return Path(override).expanduser() if override else data_dir() / ".cache"


def cache_path(*parts: str) -> Path:
    return cache_dir().joinpath(*parts)

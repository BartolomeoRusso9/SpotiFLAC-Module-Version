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

The cache half used to live at ``~/.cache/spotiflac``. The caches themselves
are not migrated — they simply rebuild themselves at the new path. The four
files in there that are *not* regenerable are (see
``adopt_legacy_cache_file``): losing them loses something the user typed.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def default_download_dir() -> str:
    """Where downloads go unless told otherwise: ``~/Music/SpotiFLAC``.

    One definition, because every frontend offers it and they must agree —
    a GUI that defaults somewhere the terminal UI does not is two libraries
    on one machine. Returned as a string: it is handed straight to the
    downloader and shown in a text field, and neither wants a Path.
    """
    return os.path.join(os.path.expanduser("~"), "Music", "SpotiFLAC")


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


#: Where the cache half lived before everything moved under ``~/.spotiflac``.
LEGACY_CACHE_DIR = Path.home() / ".cache" / "spotiflac"

#: The only files carried over from ``LEGACY_CACHE_DIR``. Everything else in
#: there is a cache that costs a re-fetch to rebuild, and nothing outside it
#: is touched at all — ``config.json`` and ``signed_sessions`` are durable
#: data with their own home, not cache that moved.
_ADOPTABLE = frozenset(
    {
        "profiles.json",
        "gui-settings.json",
        "session.json",
        "recent-fetches.json",
    }
)


def adopt_legacy_cache_file(path: Path) -> None:
    """Brings one pre-``~/.spotiflac`` file to where it is now read from.

    Called by the loader of each file in ``_ADOPTABLE`` before it reads:
    saved profiles, GUI settings, the session and the fetch history are
    things the user typed or built up, and an upgrade that silently emptied
    them reads as data loss.

    Does nothing when the file already exists at the new path (never
    overwrites what is current), when ``$SPOTIFLAC_CACHE_DIR`` is set (that
    override names a directory to use verbatim), or when there is nothing
    old to adopt. The original is copied rather than moved, so an older
    install pointed at the old directory still finds its own copy.
    """
    if path.name not in _ADOPTABLE or os.getenv("SPOTIFLAC_CACHE_DIR"):
        return
    try:
        if path.exists():
            return
        legacy = LEGACY_CACHE_DIR / path.name
        if not legacy.is_file():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, path)
    except OSError as exc:
        # Best effort by design: the caller carries on with an empty file,
        # which is what it would have done before this existed.
        logger.debug("[paths] could not adopt %s: %s", path.name, exc)
    else:
        logger.info("[paths] Carried %s over from %s", path.name, LEGACY_CACHE_DIR)

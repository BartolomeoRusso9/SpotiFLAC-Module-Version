"""Single source of truth for Mornye's on-disk locations.

New installations store state under ``~/.mornye`` (override:
``$MORNYE_DATA_DIR``). Existing ``~/.spotiflac`` data remains usable without
being moved; see ``data_dir`` and the migration guide.

    ~/.mornye/               durable data — losing it loses something real
      mornye.db
      extensions/
      signed_sessions/
      web_users.json, trusted_keys.json, registry_settings.json, ...

    ~/.mornye/.cache/        regenerable state (override: $MORNYE_CACHE_DIR)
      endpoints_cache.txt, responses/, library-index/, session.json,
      provider_priority.json, isrc-cache.json, gui-settings.json, ...

The cache half used to live at ``~/.cache/spotiflac``. Regenerable caches
rebuild at the new path; the four user-authored files are copied, never moved
or overwritten, by ``adopt_legacy_cache_file``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .env import env_value

logger = logging.getLogger(__name__)
_warned_legacy_locations: set[str] = set()


def _warn_legacy_location(kind: str, *, conflict: bool = False) -> None:
    if kind in _warned_legacy_locations:
        return
    _warned_legacy_locations.add(kind)
    if conflict:
        logger.warning(
            "Both Mornye and legacy %s locations exist; using the Mornye "
            "location and leaving the legacy data untouched.",
            kind,
        )
    else:
        logger.warning(
            "Using the existing legacy %s location to preserve user data. "
            "See the migration guide to move it after stopping Mornye.",
            kind,
        )


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
    home = Path.home()
    current = home / "Music" / "Mornye"
    legacy = home / "Music" / "SpotiFLAC"
    if current.exists():
        if legacy.exists():
            _warn_legacy_location("download", conflict=True)
        return str(current)
    if legacy.exists():
        _warn_legacy_location("download")
        return str(legacy)
    return str(current)


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

    current = Path.home() / ".mornye"
    legacy = Path.home() / ".spotiflac"
    if current.exists():
        if legacy.exists():
            _warn_legacy_location("data", conflict=True)
        return current
    if legacy.exists():
        _warn_legacy_location("data")
        return legacy
    return current


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


#: Where the cache half lived before everything moved under ``~/.mornye``.
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
    """Brings one pre-``~/.mornye`` file to where it is now read from.

    Called by the loader of each file in ``_ADOPTABLE`` before it reads:
    saved profiles, GUI settings, the session and the fetch history are
    things the user typed or built up, and an upgrade that silently emptied
    them reads as data loss.

    Does nothing when the file already exists at the new path (never
    overwrites what is current), when ``$MORNYE_CACHE_DIR`` is set (that
    override names a directory to use verbatim), or when there is nothing
    old to adopt. The original is copied rather than moved, so an older
    install pointed at the old directory still finds its own copy.
    """
    if path.name not in _ADOPTABLE or env_value("MORNYE_CACHE_DIR"):
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
        logger.warning(
            "[paths] Could not copy legacy settings file %s; the original "
            "remains untouched: %s",
            path.name,
            exc,
        )
    else:
        logger.info("[paths] Carried %s over from %s", path.name, LEGACY_CACHE_DIR)

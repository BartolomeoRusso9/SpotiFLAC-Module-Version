"""Mornye Python — a Python module with user-configured provider extensions.

Minimum use:
    from mornye import Mornye
    Mornye("URL_SPOTIFY", "./downloads")

Advanced use:
    from mornye import AsyncMornye
    # See the documentation for advanced async usage
"""

from __future__ import annotations

import importlib.metadata
import warnings
from typing import Any

# Single canonical client implementation (both sync and async)
from .client import AsyncMornye, Mornye
from .core import DownloadResult, TrackMetadata
from .core.config import (
    DownloadFailure,
    DownloadReport,
    DownloadRequest,
    DownloadSkip,
    MornyeConfig,
)
from .downloader import DownloadOptions, MornyeDownloader

# Legacy provider classes stay importable for one release, but are lazy so an
# extension-first application start never initialises their browser/session
# machinery.  New integrations should use ``mornye.extensions``.
_LEGACY_PROVIDER_EXPORTS = {
    "AmazonProvider",
    "AppleMusicProvider",
    "DeezerProvider",
    "JooxProvider",
    "KuwoProvider",
    "MiguProvider",
    "NeteaseProvider",
    "QobuzProvider",
    "TidalProvider",
}


def __getattr__(name: str) -> Any:
    if name == "SpotifyMetadataClient":
        from .core.spotify_metadata import SpotifyMetadataClient

        return SpotifyMetadataClient
    if name in _LEGACY_PROVIDER_EXPORTS:
        warnings.warn(
            f"mornye.{name} is deprecated; install and select an extension instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # The legacy `mornye.providers` package was removed. Instead of
        # attempting a hard import that would raise ModuleNotFoundError,
        # provide a clear error explaining the migration path.
        raise AttributeError(
            f"Legacy provider '{name}' is not available. Install and select an "
            "extension (ext:) or use the extensions API instead."
        )
    raise AttributeError(name)


try:
    __version__ = importlib.metadata.version("mornye")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "AsyncMornye",
    "DownloadFailure",
    "DownloadOptions",
    "DownloadReport",
    "DownloadRequest",
    "DownloadResult",
    "DownloadSkip",
    "Mornye",
    "MornyeConfig",
    "MornyeDownloader",
    "SpotifyMetadataClient",
    "TrackMetadata",
]

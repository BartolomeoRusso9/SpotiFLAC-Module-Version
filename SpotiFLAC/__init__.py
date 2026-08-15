"""SpotiFLAC — Python module for downloading high quality music.

Minimum use:
    from SpotiFLAC import SpotiFLAC
    SpotiFLAC("URL_SPOTIFY", "./downloads")

Advanced use:
    from SpotiFLAC import AsyncSpotiFLAC
    # Vedi documentazione per l'uso asincrono avanzato
"""

from __future__ import annotations

import importlib.metadata
import warnings

# Unica implementazione canonica del client (sia sincrono che asincrono)
from .client import AsyncSpotiFLAC, SpotiFLAC
from .core import DownloadResult, TrackMetadata
from .downloader import DownloadOptions, SpotiflacDownloader

# Legacy provider classes stay importable for one release, but are lazy so an
# extension-first application start never initialises their browser/session
# machinery.  New integrations should use ``SpotiFLAC.extensions``.
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


def __getattr__(name: str):
    if name == "SpotifyMetadataClient":
        from .core.spotify_metadata import SpotifyMetadataClient

        return SpotifyMetadataClient
    if name in _LEGACY_PROVIDER_EXPORTS:
        warnings.warn(
            f"SpotiFLAC.{name} is deprecated; install and select an extension instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # The legacy `SpotiFLAC.providers` package was removed. Instead of
        # attempting a hard import that would raise ModuleNotFoundError,
        # provide a clear error explaining the migration path.
        raise AttributeError(
            f"Legacy provider '{name}' is not available. Install and select an "
            "extension (ext:) or use the extensions API instead."
        )
    raise AttributeError(name)


try:
    __version__ = importlib.metadata.version("SpotiFLAC")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "AsyncSpotiFLAC",
    "DownloadOptions",
    "DownloadResult",
    "SpotiFLAC",
    "SpotiflacDownloader",
    "SpotifyMetadataClient",
    "TrackMetadata",
]

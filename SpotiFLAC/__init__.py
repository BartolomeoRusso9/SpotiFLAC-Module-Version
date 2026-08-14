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
    "AmazonProvider", "AppleMusicProvider", "DeezerProvider", "JooxProvider",
    "KuwoProvider", "MiguProvider", "NeteaseProvider", "QobuzProvider",
    "SpotifyMetadataClient", "TidalProvider",
}


def __getattr__(name: str):
    if name in _LEGACY_PROVIDER_EXPORTS:
        warnings.warn(
            f"SpotiFLAC.{name} is deprecated; install and select an extension instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from . import providers
        return getattr(providers, name)
    raise AttributeError(name)

try:
    __version__ = importlib.metadata.version("SpotiFLAC")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "AmazonProvider",
    "AppleMusicProvider",
    "AsyncSpotiFLAC",
    "DeezerProvider",
    "DownloadOptions",
    "DownloadResult",
    "JooxProvider",
    "KuwoProvider",
    "MiguProvider",
    "NeteaseProvider",
    "QobuzProvider",
    "SpotiFLAC",
    "SpotiflacDownloader",
    "SpotifyMetadataClient",
    "TidalProvider",
    "TrackMetadata",
]

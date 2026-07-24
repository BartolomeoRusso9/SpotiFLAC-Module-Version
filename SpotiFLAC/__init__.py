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

# Unica implementazione canonica del client (sia sincrono che asincrono)
from .client import AsyncSpotiFLAC, SpotiFLAC
from .core import DownloadResult, TrackMetadata
from .downloader import DownloadOptions, SpotiflacDownloader
from .providers import (
    AmazonProvider,
    AppleMusicProvider,
    DeezerProvider,
    JooxProvider,
    KuwoProvider,
    MiguProvider,
    NeteaseProvider,
    QobuzProvider,
    SpotifyMetadataClient,
    TidalProvider,
)

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

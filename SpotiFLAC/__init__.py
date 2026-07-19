"""
SpotiFLAC — Python module for downloading high quality music

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
from .client import SpotiFLAC, AsyncSpotiFLAC

from .downloader import SpotiflacDownloader, DownloadOptions
from .providers import (
    DeezerProvider,
    QobuzProvider,
    TidalProvider,
    AmazonProvider,
    AppleMusicProvider,
    JooxProvider,
    NeteaseProvider,
    MiguProvider,
    KuwoProvider,
    SpotifyMetadataClient,
)
from .core import TrackMetadata, DownloadResult

try:
    __version__ = importlib.metadata.version("SpotiFLAC")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "AsyncSpotiFLAC",
    "SpotiFLAC",
    "SpotiflacDownloader",
    "DownloadOptions",
    "DeezerProvider",
    "QobuzProvider",
    "TidalProvider",
    "AmazonProvider",
    "AppleMusicProvider",
    "JooxProvider",
    "NeteaseProvider",
    "MiguProvider",
    "KuwoProvider",
    "SpotifyMetadataClient",
    "TrackMetadata",
    "DownloadResult",
]

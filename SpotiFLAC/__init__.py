"""
SpotiFLAC — Modulo Python per il download di musica in alta fedeltà.

Uso minimo:
    from SpotiFLAC import SpotiFLAC
    SpotiFLAC("URL_SPOTIFY", "./downloads")

Uso avanzato:
    SpotiFLAC(
        url="URL_SPOTIFY",
        output_dir="./Music",
        services=["qobuz", "tidal"],
        enrich_metadata=True,
        embed_lyrics=True,
        quality="LOSSLESS"
    )

Output path (single track):
    SpotiFLAC(
        url="URL_SPOTIFY",
        output_dir="./Music",   # fallback if output_path is not set
        output_path="files/song.flac"
    )
"""
from __future__ import annotations
import logging
import sys

from .downloader import SpotiflacDownloader, DownloadOptions
from .providers import (
    DeezerProvider,
    QobuzProvider,
    TidalProvider,
    AmazonProvider,
    AppleMusicProvider,
    SpotifyMetadataClient,
)
from .core import TrackMetadata, DownloadResult

__version__ = "0.4.9"

__all__ = [
    "SpotiFLAC",
    "SpotiflacDownloader",
    "DownloadOptions",
    "DeezerProvider",
    "QobuzProvider",
    "TidalProvider",
    "AmazonProvider",
    "AppleMusicProvider",
    "SpotifyMetadataClient",
    "TrackMetadata",
    "DownloadResult",
]

def _setup_logger(level: int):
    logger = logging.getLogger("SpotiFLAC")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger

def SpotiFLAC(
        url:                   str,
        output_dir:            str,
        services:              list[str] | None = None,
        filename_format:       str              = "{title} - {artist}",
        use_track_numbers:     bool             = False,
        use_album_track_numbers: bool           = False,
        use_artist_subfolders: bool             = False,
        use_album_subfolders:  bool             = False,
        loop:                  int | None       = None,
        allow_fallback:        bool             = True,
        quality:               str              = "LOSSLESS",
        first_artist_only:     bool             = False,
        log_level:             int              = logging.WARNING,
        output_path:             str | None     = None,
        embed_lyrics:            bool           = True,
        lyrics_providers:        list[str] | None = None,
        lyrics_spotify_token:    str            = "",
        enrich_metadata:         bool           = True,
        enrich_providers:        list[str] | None = None,
        qobuz_token:             str | None     = None,
        include_featuring:     bool           = False,
) -> None:
    _setup_logger(log_level)

    opts = DownloadOptions(
        output_dir              = output_dir,
        services                = services or ["tidal"],
        filename_format         = filename_format,
        use_track_numbers       = use_track_numbers,
        use_album_track_numbers = use_album_track_numbers,
        use_artist_subfolders   = use_artist_subfolders,
        allow_fallback          = allow_fallback,
        use_album_subfolders    = use_album_subfolders,
        quality                 = quality,
        first_artist_only       = first_artist_only,
        output_path             = output_path,
        embed_lyrics            = embed_lyrics,
        lyrics_providers        = lyrics_providers or ["spotify", "musixmatch", "lrclib", "apple"],
        lyrics_spotify_token    = lyrics_spotify_token,
        enrich_metadata         = enrich_metadata,
        enrich_providers        = enrich_providers or ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
        qobuz_token             = qobuz_token,
        include_featuring       = include_featuring,
    )

    try:
        downloader = SpotiflacDownloader(opts)
        downloader.run(url, loop_minutes=loop)
    except KeyboardInterrupt:
        print("\n\n[!] Operazione interrotta dall'utente.")
    except Exception as e:
        logging.getLogger("SpotiFLAC").error("Errore critico durante l'esecuzione: %s", e)
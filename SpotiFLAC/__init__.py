"""
SpotiFLAC — Python module for downloading high quality music

Minimum use:
    from SpotiFLAC import SpotiFLAC
    SpotiFLAC("URL_SPOTIFY", "./downloads")

Advanced use:
    SpotiFLAC(
        url="URL_SPOTIFY",
        output_dir="./Music",
        services=["qobuz", "tidal"],
        enrich_metadata=True,
        embed_lyrics=True,
        quality="LOSSLESS",
        track_max_retries=2,
        post_download_action="open_folder",
    )

Batch (more URL):
    SpotiFLAC(
        url=["URL_1", "URL_2", "URL_3"],
        output_dir="./Music",
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

__version__ = "0.5.2"

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
        url:                   str | list[str],
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
        output_path:           str | None       = None,
        embed_lyrics:          bool             = True,
        lyrics_providers:      list[str] | None = None,
        enrich_metadata:       bool             = True,
        enrich_providers:      list[str] | None = None,
        qobuz_token:           str | None       = None,
        include_featuring:     bool             = False,
        track_max_retries:     int              = 0,
        post_download_action:  str              = "none",
        post_download_command: str              = "",
) -> None:
    """
    Scarica tracce/album/playlist da Spotify, Tidal, Apple Music, SoundCloud o YouTube.

    Args:
        url: URL singolo (str) o lista di URL (list[str]) per il batch.
        output_dir: Cartella di destinazione.
        services: Provider in ordine di priorità (default: ["tidal"]).
        track_max_retries: Tentativi extra per traccia in caso di fallimento (default: 0).
        post_download_action: Azione al termine — "none" | "open_folder" | "notify" | "command".
        post_download_command: Comando shell da eseguire (con {folder}, {succeeded}, {failed}).
    """
    _setup_logger(log_level)

    opts = DownloadOptions(
        output_dir              = output_dir,
        services                = services or ["tidal"],
        filename_format          = filename_format,
        use_track_numbers       = use_track_numbers,
        use_album_track_numbers = use_album_track_numbers,
        use_artist_subfolders   = use_artist_subfolders,
        allow_fallback          = allow_fallback,
        use_album_subfolders    = use_album_subfolders,
        quality                 = quality,
        first_artist_only        = first_artist_only,
        output_path             = output_path,
        embed_lyrics            = embed_lyrics,
        lyrics_providers        = lyrics_providers or ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
        enrich_metadata         = enrich_metadata,
        enrich_providers        = enrich_providers or ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
        qobuz_token             = qobuz_token,
        include_featuring       = include_featuring,
        track_max_retries       = track_max_retries,
        post_download_action    = post_download_action,
        post_download_command   = post_download_command,
    )

    try:
        downloader = SpotiflacDownloader(opts)
        downloader.run(url, loop_minutes=loop)
    except KeyboardInterrupt:
        print("\n\n[!] Operazione interrotta dall'utente.")
    except Exception as e:
        logging.getLogger("SpotiFLAC").error("Errore critico durante l'esecuzione: %s", e)
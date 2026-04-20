"""
SpotiFLAC — Production-ready Python module.

Uso minimo:
    from SpotiFLAC import SpotiFLAC
    SpotiFLAC("https://open.spotify.com/track/...", "./downloads")

Uso avanzato:
    from SpotiFLAC import SpotiFLAC
    SpotiFLAC(
        url="https://open.spotify.com/album/...",
        output_dir="./Music",
        services=["qobuz", "tidal"],
        filename_format="{year} - {album}/{track}. {title}",
        use_artist_subfolders=True,
        use_album_subfolders=True,
        loop=60,
    )
"""
from __future__ import annotations
import logging

from .downloader import SpotiflacDownloader, DownloadOptions
from .providers import (
    QobuzProvider,
    TidalProvider,
    SpotifyMetadataClient,
)
from .core import TrackMetadata, DownloadResult

__version__ = "2.0.0"

__all__ = [
    "SpotiFLAC",
    "SpotiflacDownloader",
    "DownloadOptions",
    "QobuzProvider",
    "TidalProvider",
    "SpotifyMetadataClient",
    "TrackMetadata",
    "DownloadResult",
]


def SpotiFLAC(
    url:                   str,
    output_dir:            str,
    services:              list[str] | None = None,
    filename_format:       str              = "{title} - {artist}",
    use_track_numbers:     bool             = False,
    use_artist_subfolders: bool             = False,
    use_album_subfolders:  bool             = False,
    loop:                  int | None       = None,
    quality:               str              = "LOSSLESS",
    first_artist_only:     bool             = False,
    log_level:             int              = logging.WARNING,
) -> None:
    """
    Scarica tracce Spotify in FLAC dai provider configurati.

    Args:
        url:                   URL Spotify (track, album, playlist).
        output_dir:            Directory di destinazione.
        services:              Lista provider in ordine di priorità.
                               Valori: "tidal", "qobuz", "spoti", "deezer", "amazon", "youtube".
        filename_format:       Template nome file. Placeholder: {title}, {artist}, {album},
                               {album_artist}, {year}, {date}, {track}, {disc}, {isrc}.
        use_track_numbers:     Prefissa il filename con il numero traccia.
        use_artist_subfolders: Organizza per sottocartella artista.
        use_album_subfolders:  Organizza per sottocartella album.
        loop:                  Minuti di attesa prima di rieseguire. None = singolo run.
        quality:               Qualità per Tidal ("LOSSLESS", "HI_RES") e Qobuz ("6", "7", "27").
        first_artist_only:     Usa solo il primo artista nei tag e filename.
        log_level:             Livello di logging (default: WARNING).
    """
    logging.basicConfig(
        level  = log_level,
        format = "[%(levelname)s] %(name)s: %(message)s",
    )

    opts = DownloadOptions(
        output_dir            = output_dir,
        services              = services or ["tidal"],
        filename_format       = filename_format,
        use_track_numbers     = use_track_numbers,
        use_artist_subfolders = use_artist_subfolders,
        use_album_subfolders  = use_album_subfolders,
        quality               = quality,
        first_artist_only     = first_artist_only,
    )

    try:
        downloader = SpotiflacDownloader(opts)
        downloader.run(url, loop_minutes=loop)
    except KeyboardInterrupt:
        print("\n\n[!] Download stopped by user.")

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
        quality="HI_RES"
    )
"""
from __future__ import annotations
import logging
import sys

from .downloader import SpotiflacDownloader, DownloadOptions
from .providers import (
    QobuzProvider,
    TidalProvider,
    AmazonProvider,
    SpotifyMetadataClient,
)
from .core import TrackMetadata, DownloadResult

__version__ = "0.3.9"

__all__ = [
    "SpotiFLAC",
    "SpotiflacDownloader",
    "DownloadOptions",
    "QobuzProvider",
    "TidalProvider",
    "AmazonProvider",
    "SpotifyMetadataClient",
    "TrackMetadata",
    "DownloadResult",
]

def _setup_logger(level: int):
    """Configura il logging per il namespace SpotiFLAC senza disturbare il root logger."""
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
        quality:               str              = "LOSSLESS",
        first_artist_only:     bool             = False,
        log_level:             int              = logging.WARNING,
        # Opzioni Lyrics (Attive di default)
        embed_lyrics:            bool           = True,
        lyrics_providers:        list[str] | None = None,
        lyrics_spotify_token:    str            = "",
        # Opzioni Enrichment (Attive di default)
        enrich_metadata:         bool           = True,
        enrich_providers:        list[str] | None = None,
        qobuz_token:             str | None     = None,
) -> None:
    """
    Interfaccia principale per scaricare tracce Spotify in formato FLAC.

    Args:
        url: URL Spotify (track, album, playlist).
        output_dir: Cartella di destinazione.
        services: Provider audio in ordine di priorità ("tidal", "qobuz", "amazon").
        filename_format: Template per il nome file.
        use_track_numbers: Aggiunge il numero traccia all'inizio del nome file.
        use_artist_subfolders: Organizza in cartelle per artista.
        use_album_subfolders: Organizza in sottocartelle per album.
        loop: Se impostato (int), ripete l'operazione ogni N minuti.
        quality: Qualità audio desiderata ("LOSSLESS" o "HI_RES").
        first_artist_only: Usa solo il primo artista nei tag e nel nome file.
        log_level: Livello di dettaglio log (logging.INFO, DEBUG, WARNING).
        embed_lyrics: Scarica e inserisce i testi nel file FLAC.
        lyrics_providers: Lista provider testi.
        enrich_metadata: Arricchisce i tag con dati extra (BPM, Label, Genre, ecc.).
        enrich_providers: Provider per i dati extra.
        qobuz_token: Token utente Qobuz opzionale.
    """
    # 1. Setup del logging
    _setup_logger(log_level)

    # 2. Preparazione opzioni con gestione dei default
    opts = DownloadOptions(
        output_dir            = output_dir,
        services              = services or ["tidal"],
        filename_format       = filename_format,
        use_track_numbers     = use_track_numbers,
        use_album_track_numbers = use_album_track_numbers,
        use_artist_subfolders = use_artist_subfolders,
        use_album_subfolders  = use_album_subfolders,
        quality               = quality,
        first_artist_only     = first_artist_only,
        # Lyrics
        embed_lyrics            = embed_lyrics,
        lyrics_providers        = lyrics_providers or ["spotify", "musixmatch", "lrclib", "apple"],
        lyrics_spotify_token    = lyrics_spotify_token,
        # Enrichment
        enrich_metadata         = enrich_metadata,
        enrich_providers        = enrich_providers or ["deezer", "apple", "qobuz", "tidal"],
        qobuz_token             = qobuz_token,
    )

    # 3. Esecuzione
    try:
        downloader = SpotiflacDownloader(opts)
        downloader.run(url, loop_minutes=loop)
    except KeyboardInterrupt:
        print("\n\n[!] Operazione interrotta dall'utente.")
    except Exception as e:
        logging.getLogger("SpotiFLAC").error("Errore critico durante l'esecuzione: %s", e)
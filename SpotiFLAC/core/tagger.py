"""
Tagger FLAC centralizzato.
Rimuove la duplicazione di _embed_metadata presente in qobuzDL, tidalDL, spotidownloaderDL.
"""
from __future__ import annotations
import logging
from pathlib import Path

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

from .errors import SpotiflacError, ErrorKind
from .models import TrackMetadata

logger = logging.getLogger(__name__)

SOURCE_TAG = "https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"


def embed_metadata(
    filepath:          str | Path,
    metadata:          TrackMetadata,
    *,
    first_artist_only: bool  = False,
    cover_url:         str   = "",
    cover_data:        bytes | None = None,
    session:           requests.Session | None = None,
    extra_tags:        dict[str, str] | None = None,
    multi_artist:      bool  = True,
    embed_lyrics:      bool = False,
) -> None:
    """
    Scrive i tag Vorbis Comment in un file FLAC e opzionalmente
    embeds la copertina da URL o da bytes già scaricati.

    Raises SpotiflacError(FILE_IO) se mutagen fallisce.
    """
    path = Path(filepath)
    if not path.exists():
        raise SpotiflacError(ErrorKind.FILE_IO, f"File not found: {path}")

    # Scarica cover se necessario
    if not cover_data and cover_url:
        cover_data = _fetch_cover(cover_url, session)

    try:
        audio = FLAC(str(path))
        audio.delete()  # rimuove tag preesistenti

        tags = metadata.as_flac_tags(first_artist_only=first_artist_only)
        tags["DESCRIPTION"] = SOURCE_TAG

        lyrics: str | None = None
        if embed_lyrics and metadata.title and metadata.first_artist:
            from .lyrics import fetch_lyrics
            try:
                lyrics = fetch_lyrics(
                    metadata.title,
                    metadata.first_artist,
                    metadata.album,
                    metadata.duration_ms // 1000,
                )
            except Exception as exc:
                logger.warning("Lyrics fetch failed for %s: %s", path.name, exc)
        if lyrics:
            tags["LYRICS"] = lyrics
            logger.debug("Lyrics embedded: %s chars", len(lyrics))

        if extra_tags:
            tags.update(extra_tags)
        for key, val in tags.items():
        # ARTIST e ALBUMARTIST: scrivi un tag per artista se multi_artist=True
            if multi_artist and key in ("ARTIST", "ALBUMARTIST") and "," in val:
                artists = [a.strip() for a in val.split(",") if a.strip()]
                audio[key] = artists      # mutagen accetta lista → tag multipli
            else:
                audio[key] = val

        if cover_data:
            pic          = Picture()
            pic.data     = cover_data
            pic.type     = PictureType.COVER_FRONT
            pic.mime     = "image/jpeg"
            audio.add_picture(pic)

        audio.save()
        logger.debug("Metadata embedded: %s", path.name)

    except SpotiflacError:
        raise
    except Exception as exc:
        raise SpotiflacError(
            ErrorKind.FILE_IO,
            f"Failed to embed metadata in {path.name}: {exc}",
            cause=exc,
        )


def _fetch_cover(url: str, session: requests.Session | None) -> bytes | None:
    """
    Scarica la cover art. Ritorna None silenziosamente in caso di errore
    (la cover mancante non deve bloccare il download).
    """
    if not url:
        return None
    try:
        s   = session or requests.Session()
        res = s.get(url, timeout=15)
        if res.status_code == 200:
            return res.content
        logger.warning("Cover download returned HTTP %s for %s", res.status_code, url)
    except Exception as exc:
        logger.warning("Cover download failed (%s): %s", url, exc)
    return None


def max_resolution_spotify_cover(url: str) -> str:
    """
    Converte un URL immagine Spotify nella variante a massima risoluzione.
    """
    import re
    if "i.scdn.co/image/" in url:
        return re.sub(r"(ab67616d0000)[a-z0-9]+", r"\g<1>b273", url)
    return url

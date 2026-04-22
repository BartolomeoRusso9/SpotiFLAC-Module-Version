# SpotiFLAC/core/tagger.py
"""
Tagger FLAC centralizzato — con metadata enrichment multi-provider e lyrics multi-provider.
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
        # Lyrics options
        embed_lyrics:         bool = False,
        lyrics_providers:     list[str] | None = None,
        lyrics_spotify_token: str = "",
        lyrics_musixmatch_token: str = "",
        # Metadata enrichment options
        enrich:           bool = False,
        enrich_providers: list[str] | None = None,
        enrich_qobuz_token: str  = "",
) -> None:
    """
    Scrive i tag Vorbis Comment in un file FLAC, opzionalmente embed cover,
    testi (da più provider) e metadati arricchiti (label, BPM, genere…).

    Args:
        filepath:                Path del file FLAC.
        metadata:                TrackMetadata Spotify.
        first_artist_only:       Usa solo il primo artista.
        cover_url:               URL copertina (scaricata se cover_data è None).
        cover_data:              Byte della copertina (già scaricata).
        session:                 requests.Session riutilizzabile.
        extra_tags:              Tag aggiuntivi liberi.
        multi_artist:            Scrivi ARTIST/ALBUMARTIST multipli.
        embed_lyrics:            Abilita fetch testi multi-provider.
        lyrics_providers:        Lista provider testi in ordine.
                                 Default: ["spotify","musixmatch","amazon","lrclib"]
        lyrics_spotify_token:    Cookie sp_dc Spotify (per lyrics Spotify).
        lyrics_musixmatch_token: Token Musixmatch desktop.
        enrich:                  Abilita metadata enrichment multi-provider.
        enrich_providers:        Lista provider enrichment in ordine.
                                 Default: ["deezer","apple","qobuz","tidal"]
        enrich_qobuz_token:      Token per l'API di Qobuz.
    """
    path = Path(filepath)
    if not path.exists():
        raise SpotiflacError(ErrorKind.FILE_IO, f"File not found: {path}")

    # ------------------------------------------------------------------ #
    # 1. Metadata enrichment                                               #
    # ------------------------------------------------------------------ #
    enriched_tags: dict[str, str] = {}
    enriched_cover_url: str = ""

    if enrich:
        try:
            from .metadata_enrichment import enrich_metadata as _enrich
            enriched = _enrich(
                track_name  = metadata.title,
                artist_name = metadata.first_artist,
                isrc        = metadata.isrc,
                providers   = enrich_providers,
                qobuz_token = enrich_qobuz_token,
            )
            enriched_tags      = enriched.as_tags()
            enriched_cover_url = enriched.cover_url_hd
            if enriched._sources:
                nomi_campi = {"cover_url_hd": "cover", "explicit": "advisory"}
                dettagli = ", ".join(
                    f"{nomi_campi.get(campo, campo)} ({provider})"
                    for campo, provider in enriched._sources.items()
                )
                print(f"Arricchito con: {dettagli}")
            logger.debug("[tagger] enriched: %s", list(enriched_tags.keys()))
        except Exception as exc:
            logger.warning("[tagger] enrichment failed: %s", exc)

    # ------------------------------------------------------------------ #
    # 2. Cover art                                                         #
    # ------------------------------------------------------------------ #
    if not cover_data:
        # Preferisci cover HD dall'enrichment, poi quella di Spotify
        best_cover = enriched_cover_url or cover_url or metadata.cover_url
        if best_cover:
            cover_data = _fetch_cover(best_cover, session)

    # ------------------------------------------------------------------ #
    # 3. Lyrics                                                            #
    # ------------------------------------------------------------------ #
    lyrics: str | None = None

    if embed_lyrics and metadata.title and metadata.first_artist:
        try:
            from .lyrics import fetch_lyrics
            lyrics = fetch_lyrics(
                track_name       = metadata.title,
                artist_name      = metadata.first_artist,
                album_name       = metadata.album,
                duration_s       = metadata.duration_ms // 1000,
                track_id         = metadata.id,
                isrc             = metadata.isrc,
                providers        = lyrics_providers,
                spotify_token    = lyrics_spotify_token,
                musixmatch_token = lyrics_musixmatch_token,
            )
        except Exception as exc:
            logger.warning("[tagger] lyrics fetch failed: %s", exc)

    # ------------------------------------------------------------------ #
    # 4. Write FLAC tags                                                   #
    # ------------------------------------------------------------------ #
    try:
        audio = FLAC(str(path))
        audio.delete()

        tags = metadata.as_flac_tags(first_artist_only=first_artist_only)
        tags["DESCRIPTION"] = SOURCE_TAG

        # Enriched tags hanno precedenza su extra_tags solo per campi assenti
        merged_extra: dict[str, str] = {**enriched_tags}
        if extra_tags:
            merged_extra.update(extra_tags)  # extra_tags espliciti vincono sempre

        if lyrics:
            tags["LYRICS"] = lyrics
            logger.debug("[tagger] lyrics embedded (%d chars)", len(lyrics))

        if merged_extra:
            tags.update(merged_extra)

        for key, val in tags.items():
            if multi_artist and key in ("ARTIST", "ALBUMARTIST") and "," in val:
                artists = [a.strip() for a in val.split(",") if a.strip()]
                audio[key] = artists
            else:
                audio[key] = val

        if cover_data:
            pic          = Picture()
            pic.data     = cover_data
            pic.type     = PictureType.COVER_FRONT
            pic.mime     = "image/jpeg"
            audio.add_picture(pic)

        audio.save()
        logger.debug("[tagger] metadata embedded: %s", path.name)

    except SpotiflacError:
        raise
    except Exception as exc:
        raise SpotiflacError(
            ErrorKind.FILE_IO,
            f"Failed to embed metadata in {path.name}: {exc}",
            cause=exc,
        )


def _fetch_cover(url: str, session: requests.Session | None) -> bytes | None:
    if not url:
        return None
    try:
        s   = session or requests.Session()
        res = s.get(url, timeout=15)
        if res.status_code == 200:
            return res.content
        logger.warning("[tagger] cover HTTP %s for %s", res.status_code, url)
    except Exception as exc:
        logger.warning("[tagger] cover download failed (%s): %s", url, exc)
    return None


def max_resolution_spotify_cover(url: str) -> str:
    """Converte URL immagine Spotify alla variante massima risoluzione."""
    import re
    if "i.scdn.co/image/" in url:
        return re.sub(r"(ab67616d0000)[a-z0-9]+", r"\g<1>b273", url)
    return url
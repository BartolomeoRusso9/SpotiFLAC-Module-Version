# SpotiFLAC/core/tagger.py
"""
Tagger FLAC centralizzato — con metadata enrichment multi-provider e lyrics multi-provider.

FIX v2:
  - ARTIST/ALBUMARTIST scritti come stringa unica con virgola (non lista)
    per compatibilità con tutti gli editor (beets, foobar2000, picard, ecc.)
  - Aggiunto ARTISTS/ALBUMARTISTS come tag multi-valore separato
    per editor avanzati che li supportano (MusicBrainz Picard standard)
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


# ---------------------------------------------------------------------------
# MusicBrainz summary helper
# ---------------------------------------------------------------------------

def _print_mb_summary(mb_tags: dict) -> None:
    """
    Stampa un riepilogo dei tag aggiunti da MusicBrainz,
    in formato analogo al messaggio "Arricchito con: ..." del tagger.
    """
    if not mb_tags:
        return

    # Mappatura unificata sia per i tag FLAC (UPPERCASE) sia per i dizionari raw (lowercase)
    _TAG_LABELS = {
        "GENRE": "genere", "genre": "genere",
        "BPM": "BPM", "bpm": "BPM",
        "LABEL": "etichetta", "label": "etichetta",
        "CATALOGNUMBER": "n. catalogo", "catalognumber": "n. catalogo",
        "BARCODE": "barcode", "barcode": "barcode",
        "ORIGINALDATE": "data", "original_date": "data",
        "RELEASECOUNTRY": "paese", "country": "paese",
        "RELEASESTATUS": "stato release", "status": "stato release",
        "MEDIA": "supporto", "media": "supporto",
        "RELEASETYPE": "tipo release", "type": "tipo release",
        "ARTISTSORT": "artista (sort)", "artist_sort": "artista (sort)",
        "ALBUMARTISTSORT": "artista album (sort)", "albumartist_sort": "artista album (sort)",
        "SCRIPT": "scrittura", "script": "scrittura",
    }

    # Raggruppa tutti gli ID per mostrarli in un unico conteggio finale
    mb_ids = {
        k: v for k, v in mb_tags.items()
        if str(k).startswith("MUSICBRAINZ_") or str(k).startswith("mbid_")
    }

    # Ignoriamo l'anno per evitare il duplicato a schermo (teniamo solo ORIGINALDATE/data)
    skip_dupes = {"ORIGINALYEAR", "original_year", "DATE", "date"}

    # Selezioniamo solo i campi che hanno effettivamente un valore e non sono ID o duplicati
    important = {
        k: v for k, v in mb_tags.items()
        if k not in mb_ids and k not in skip_dupes and v
    }

    parts = []
    for tag, val in important.items():
        label = _TAG_LABELS.get(tag, str(tag).lower())
        # Tronchiamo i valori troppo lunghi a 40 caratteri (es. per stringhe lunghissime)
        short_val = str(val)[:40] + ("…" if len(str(val)) > 40 else "")
        parts.append(f"{label}: {short_val}")

    if mb_ids:
        parts.append(f"ID MusicBrainz ({len(mb_ids)} campi)")

    if parts:
        print(f"  ✦ MusicBrainz: {', '.join(parts)}")


# ---------------------------------------------------------------------------
# Main embed function
# ---------------------------------------------------------------------------

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
        # Metadata enrichment options
        enrich:           bool = False,
        enrich_providers: list[str] | None = None,
        enrich_qobuz_token: str  = "",
) -> None:
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
        best_cover = enriched_cover_url or cover_url or metadata.cover_url
        if best_cover:
            cover_data = _fetch_cover(best_cover, session)

    # ------------------------------------------------------------------ #
    # 3. Lyrics                                                            #
    # ------------------------------------------------------------------ #
    lyrics: str | None = None
    lyrics_prov: str = ""

    if embed_lyrics and metadata.title and metadata.first_artist:
        try:
            from .lyrics import fetch_lyrics
            res = fetch_lyrics(
                track_name       = metadata.title,
                artist_name      = metadata.first_artist,
                album_name       = metadata.album,
                duration_s       = metadata.duration_ms // 1000,
                track_id         = metadata.id,
                isrc             = metadata.isrc,
                providers        = lyrics_providers,
                spotify_token    = lyrics_spotify_token,
            )
            # Supportiamo sia la nuova tupla (lyrics, provider) sia la vecchia stringa
            if isinstance(res, tuple):
                lyrics, lyrics_prov = res
            else:
                lyrics = res
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

        # Uniamo i tag dell'arricchimento (Deezer/Apple) con quelli MusicBrainz
        merged_extra: dict[str, str] = {**enriched_tags}

        if extra_tags:
            merged_extra.update(extra_tags)

        # --- LOGICA SINGOLI ---
        if metadata.total_tracks <= 2:
            enrich_genre = enriched_tags.get("GENRE")
            if enrich_genre:
                tags["GENRE"] = enrich_genre
                # Rimuoviamo GENRE in qualsiasi forma (maiuscolo/minuscolo)
                keys_to_remove = [k for k in merged_extra if k.upper() == "GENRE"]
                for k in keys_to_remove:
                    del merged_extra[k]

        # Scriviamo tutti i tag extra (Enrichment + MusicBrainz)
        if merged_extra:
            if metadata.composer:
                merged_extra.pop("COMPOSER", None)
                merged_extra.pop("composer", None)
            if metadata.copyright:
                merged_extra.pop("COPYRIGHT", None)
                merged_extra.pop("copyright", None)
            orig_date = merged_extra.get("original_date") or merged_extra.get("ORIGINALDATE")
            if orig_date:
                tags["ORIGINALDATE"] = str(orig_date)
                tags["ORIGINALYEAR"] = str(orig_date)[:4]

            _date_keys = {"ORIGINAL_DATE", "ORIGINAL_YEAR", "ORIGINALDATE", "ORIGINALYEAR",
                          "original_date", "original_year"}

            for key, val in merged_extra.items():
                if key not in _date_keys and key.upper() not in _date_keys:
                    tags[key.upper()] = str(val)

        if lyrics:
            tags["LYRICS"] = lyrics
            prov_str = lyrics_prov if lyrics_prov else "sconosciuto"
            print(f"  ✦ Testo: aggiunto tramite {prov_str}")
            logger.debug("[tagger] lyrics embedded (%d chars)", len(lyrics))

        for key, val in tags.items():
            if multi_artist and key in ("ARTIST", "ALBUMARTIST") and "," in val:
                parts = [a.strip() for a in val.split(",") if a.strip()]

                # FIX: ARTIST = stringa unica "The Weeknd, Playboi Carti"
                audio[key] = val

                # ARTISTS / ALBUMARTISTS = tag multi-valore per editor moderni
                audio[key + "S"] = parts
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
        res = s.get(url, timeout=8)
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
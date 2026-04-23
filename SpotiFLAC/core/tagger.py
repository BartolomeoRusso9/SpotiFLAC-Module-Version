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

    Esempio output:
      ✦ MusicBrainz: genere: Electronic; R&B, BPM: 128, label: Republic Records,
                     data originale: 2016-11-25, paese: US, ID MusicBrainz (3 campi)
    """
    if not mb_tags:
        return

    _TAG_LABELS = {
        "GENRE":                      "genere",
        "BPM":                        "BPM",
        "LABEL":                      "label",
        "ORGANIZATION":               "etichetta",
        "DATE":                       "data originale",
        "ORIGINALDATE":               "data originale",
        "MUSICBRAINZ_TRACKID":        "MB track",
        "MUSICBRAINZ_ALBUMID":        "MB album",
        "MUSICBRAINZ_ARTISTID":       "MB artist",
        "MUSICBRAINZ_RELEASEGROUPID": "MB rel.group",
        "BARCODE":                    "barcode",
        "RELEASECOUNTRY":             "paese",
        "RELEASESTATUS":              "stato release",
        "MEDIA":                      "supporto",
        "RELEASETYPE":                "tipo release",
        "ARTISTSORT":                 "artista (sort)",
        "SCRIPT":                     "script",
    }

    mb_ids     = {k: v for k, v in mb_tags.items() if k.startswith("MUSICBRAINZ_")}
    skip_dupes = {"ORIGINALDATE", "ORIGINALYEAR"}  # alias di DATE, già mostrati tramite DATE
    important  = {k: v for k, v in mb_tags.items()
                  if k not in mb_ids and k not in skip_dupes}

    parts = []
    for tag, val in important.items():
        label     = _TAG_LABELS.get(tag, tag.lower())
        short_val = str(val)[:35] + ("…" if len(str(val)) > 35 else "")
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
        lyrics_musixmatch_token: str = "",
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

        merged_extra: dict[str, str] = {**enriched_tags}
        if extra_tags:
            merged_extra.update(extra_tags)

        if merged_extra:
            if "original_date" in merged_extra and merged_extra["original_date"]:
                tags["ORIGINALDATE"] = merged_extra["original_date"]
                tags["ORIGINALYEAR"] = merged_extra["original_date"][:4]

            mb_mapping = {
                "mbid_track":    "MUSICBRAINZ_TRACKID",
                "mbid_album":    "MUSICBRAINZ_ALBUMID",
                "mbid_artist":   "MUSICBRAINZ_ARTISTID",
                "mbid_relgroup": "MUSICBRAINZ_RELEASEGROUPID",
                "barcode":       "BARCODE",
                "label":         "LABEL",
                "country":       "RELEASECOUNTRY",
                "status":        "RELEASESTATUS",
                "media":         "MEDIA",
                "type":          "RELEASETYPE",
                "artist_sort":   "ARTISTSORT",
                "script":        "SCRIPT",
                "bpm":           "BPM",
                "genre":         "GENRE",
            }

            for mb_key, tag_name in mb_mapping.items():
                val = merged_extra.get(mb_key)
                if val:
                    tags[tag_name] = str(val)

        if lyrics:
            tags["LYRICS"] = lyrics
            logger.debug("[tagger] lyrics embedded (%d chars)", len(lyrics))

        if merged_extra:
            tags.update(merged_extra)

        for key, val in tags.items():
            if multi_artist and key in ("ARTIST", "ALBUMARTIST") and "," in val:
                parts = [a.strip() for a in val.split(",") if a.strip()]

                # FIX: ARTIST = stringa unica "The Weeknd, Playboi Carti"
                # (se fosse una lista, editor come beets/foobar2000 concatenano SENZA separatore)
                audio[key] = val

                # ARTISTS / ALBUMARTISTS = tag multi-valore per editor moderni (Picard standard)
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
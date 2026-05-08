# SpotiFLAC/core/tagger.py
"""
Tagger centralizzato — supporto FLAC e MP3.

FLAC → tag Vorbis Comment via mutagen.flac
MP3  → tag ID3v2 via mutagen.id3

Entrambi i formati condividono la stessa pipeline:
  1. Metadata enrichment (Deezer / Apple / Qobuz / Tidal / SoundCloud)
  2. Cover art (HD se disponibile)
  3. Lyrics multi-provider
  4. MusicBrainz (passati come extra_tags)
  5. Scrittura tag sul file
"""
from __future__ import annotations
import logging
from pathlib import Path

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3, ID3NoHeaderError, PictureType as _PictureType,
    TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TPOS, APIC,
    TPUB, TCOM, TCOP, TCON, TBPM, TSRC, TDOR,
    TSOP, TSO2, WXXX, COMM, USLT, TXXX,
)
from mutagen.id3 import PictureType as ID3PictureType
from mutagen.id3 import PictureType
from mutagen.flac import Picture as FlacPicture
from dataclasses import dataclass, field

from .errors import SpotiflacError, ErrorKind
from .models import TrackMetadata

logger = logging.getLogger(__name__)

SOURCE_TAG = "https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"

# ---------------------------------------------------------------------------
# FLAC tag → ID3 frame mapping
# ---------------------------------------------------------------------------

# Vorbis tag  →  (ID3FrameClass, kwargs_override | None)
# Se il valore è None il tag viene scritto come TXXX con desc=chiave originale.
_FLAC_TO_ID3: dict[str, tuple | None] = {
    "TITLE":              (TIT2,  {}),
    "ARTIST":             (TPE1,  {}),
    "ALBUM":              (TALB,  {}),
    "ALBUMARTIST":        (TPE2,  {}),
    "DATE":               (TDRC,  {}),
    "TRACKNUMBER":        None,                  # gestito a parte (TRCK)
    "TRACKTOTAL":         None,                  # parte di TRCK
    "DISCNUMBER":         None,                  # gestito a parte (TPOS)
    "DISCTOTAL":          None,                  # parte di TPOS
    "ISRC":               (TSRC,  {}),
    "COPYRIGHT":          (TCOP,  {}),
    "COMPOSER":           (TCOM,  {}),
    "ORGANIZATION":       (TPUB,  {}),
    "LABEL":              (TPUB,  {}),
    "GENRE":              (TCON,  {}),
    "BPM":                (TBPM,  {}),
    "ORIGINALDATE":       (TDOR,  {}),
    "ARTISTSORT":         (TSOP,  {}),
    "ALBUMARTISTSORT":    (TSO2,  {}),
    # URL → WXXX con desc vuota
    "URL":                None,
    # Tutto il resto → TXXX
}

# Tag che finiscono in TXXX con la chiave come desc
_TXXX_TAGS = {
    "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_ARTISTID",
    "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ_ALBUMARTISTID",
    "BARCODE",
    "CATALOGNUMBER",
    "RELEASECOUNTRY",
    "RELEASESTATUS",
    "RELEASETYPE",
    "MEDIA",
    "SCRIPT",
    "ORIGINALYEAR",
    "ITUNESADVISORY",
    "UPC",
    "DESCRIPTION",
    "ARTISTS",
    "ALBUMARTISTS",
}


# ---------------------------------------------------------------------------
# MusicBrainz summary helper
# ---------------------------------------------------------------------------

def _print_mb_summary(mb_tags: dict) -> None:
    if not mb_tags:
        return

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

    mb_ids = {
        k: v for k, v in mb_tags.items()
        if str(k).startswith("MUSICBRAINZ_") or str(k).startswith("mbid_")
    }
    skip_dupes = {"ORIGINALYEAR", "original_year", "DATE", "date"}
    important = {
        k: v for k, v in mb_tags.items()
        if k not in mb_ids and k not in skip_dupes and v
    }

    parts = []
    for tag, val in important.items():
        label = _TAG_LABELS.get(tag, str(tag).lower())
        short_val = str(val)[:40] + ("…" if len(str(val)) > 40 else "")
        parts.append(f"{label}: {short_val}")

    if mb_ids:
        parts.append(f"ID MusicBrainz ({len(mb_ids)} campi)")

    if parts:
        print(f"  ✦ MusicBrainz: {', '.join(parts)}")


# ---------------------------------------------------------------------------
# Internal: write ID3 tags to an MP3 file
# ---------------------------------------------------------------------------

def _embed_id3(
        path:        Path,
        tags:        dict[str, str],
        cover_data:  bytes | None,
        lyrics:      str | None,
        lyrics_prov: str,
) -> None:
    """Scrive tutti i tag ID3 su un file MP3."""
    try:
        audio = ID3(str(path))
        audio.delete()
    except ID3NoHeaderError:
        audio = ID3()

    # ── numeri traccia e disco ──────────────────────────────────────────────
    track_num   = tags.get("TRACKNUMBER", "0")
    track_total = tags.get("TRACKTOTAL",  "0")
    disc_num    = tags.get("DISCNUMBER",  "1")
    disc_total  = tags.get("DISCTOTAL",   "1")

    trck = f"{track_num}/{track_total}" if track_total and track_total != "0" else track_num
    tpos = f"{disc_num}/{disc_total}"   if disc_total  and disc_total  != "1" else disc_num

    audio.add(TRCK(encoding=3, text=trck))
    audio.add(TPOS(encoding=3, text=tpos))

    # ── tag con frame dedicato ─────────────────────────────────────────────
    _FRAME_MAP: dict[str, type] = {
        "TITLE":           TIT2,
        "ARTIST":          TPE1,
        "ALBUM":           TALB,
        "ALBUMARTIST":     TPE2,
        "DATE":            TDRC,
        "ISRC":            TSRC,
        "COPYRIGHT":       TCOP,
        "COMPOSER":        TCOM,
        "ORGANIZATION":    TPUB,
        "LABEL":           TPUB,   # alias — uno sovrascrive l'altro (ok)
        "GENRE":           TCON,
        "BPM":             TBPM,
        "ORIGINALDATE":    TDOR,
        "ARTISTSORT":      TSOP,
        "ALBUMARTISTSORT": TSO2,
    }
    skip = {"TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL", "URL", "DESCRIPTION"}

    for key, val in tags.items():
        key_up = key.upper()
        if key_up in skip or not val:
            continue

        if key_up in _FRAME_MAP:
            frame_cls = _FRAME_MAP[key_up]
            frame_id  = frame_cls.__name__
            audio.add(frame_cls(encoding=3, text=str(val)))

        elif key_up == "URL":
            audio.add(WXXX(encoding=3, desc="", url=str(val)))

        elif key_up in _TXXX_TAGS or key_up.startswith("MUSICBRAINZ_"):
            audio.add(TXXX(encoding=3, desc=key_up, text=str(val)))

        else:
            # Fallback generico → TXXX
            audio.add(TXXX(encoding=3, desc=key_up, text=str(val)))

    # ── commento / source tag ──────────────────────────────────────────────
    audio.add(COMM(encoding=3, lang="eng", desc="", text=[SOURCE_TAG]))

    # ── URL se presente ────────────────────────────────────────────────────
    if tags.get("URL"):
        audio.add(WXXX(encoding=3, desc="", url=tags["URL"]))

    # ── lyrics ─────────────────────────────────────────────────────────────
    if lyrics and lyrics.strip():
        audio.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        prov_str = lyrics_prov if lyrics_prov else "sconosciuto"
        print(f"  ✦ Testo: aggiunto tramite {prov_str}")
        logger.debug("[tagger/mp3] lyrics embedded (%d chars)", len(lyrics))

    # ── copertina ──────────────────────────────────────────────────────────
    if cover_data:
        audio.add(APIC(
            encoding = 3,
            mime     = "image/jpeg",
            type     = ID3PictureType.COVER_FRONT,
            desc     = "Cover",
            data     = cover_data,
        ))

    audio.save(str(path), v2_version=3)
    logger.debug("[tagger/mp3] tags written: %s", path.name)


# ---------------------------------------------------------------------------
# Internal: write Vorbis Comment tags to a FLAC file
# ---------------------------------------------------------------------------

def _embed_flac(
        path:        Path,
        tags:        dict[str, str],
        cover_data:  bytes | None,
        lyrics:      str | None,
        lyrics_prov: str,
        multi_artist: bool,
) -> None:
    """Scrive tutti i tag Vorbis Comment su un file FLAC."""
    audio = FLAC(str(path))
    audio.delete()

    if lyrics and lyrics.strip():
        tags["LYRICS"] = lyrics
        prov_str = lyrics_prov if lyrics_prov else "sconosciuto"
        print(f"  ✦ Testo: aggiunto tramite {prov_str}")
        logger.debug("[tagger/flac] lyrics embedded (%d chars)", len(lyrics))

    for key, val in tags.items():
        if multi_artist and key in ("ARTIST", "ALBUMARTIST") and "," in val:
            parts = [a.strip() for a in val.split(",") if a.strip()]
            audio[key] = val
            audio[key + "S"] = parts
        else:
            audio[key] = val

    if cover_data:
        pic          = FlacPicture()
        pic.data     = cover_data
        pic.type     = PictureType.COVER_FRONT
        pic.mime     = "image/jpeg"
        audio.add_picture(pic)

    audio.save()
    logger.debug("[tagger/flac] tags written: %s", path.name)

@dataclass
class EmbedOptions:
    first_artist_only:    bool            = False
    cover_url:            str             = ""
    embed_lyrics:         bool            = False
    lyrics_providers:     list[str]       = field(default_factory=list)
    lyrics_spotify_token: str             = ""
    enrich:               bool            = False
    enrich_providers:     list[str] | None = None
    enrich_qobuz_token:   str             = ""
    is_album:             bool            = False
    extra_tags:           dict[str, str]  = field(default_factory=dict)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def embed_metadata(
        filepath:          str | Path,
        metadata:          TrackMetadata,
        opts:              EmbedOptions,
        *,
        cover_data:        bytes | None = None,
        session:           requests.Session | None = None,
        multi_artist:      bool  = True,
) -> None:
    path = Path(filepath)
    if not path.exists():
        raise SpotiflacError(ErrorKind.FILE_IO, f"File not found: {path}")

    is_mp3  = path.suffix.lower() == ".mp3"
    is_flac = path.suffix.lower() == ".flac"

    if not is_mp3 and not is_flac:
        logger.warning("[tagger] formato non supportato: %s — skip", path.suffix)
        return

    # ── 1. Metadata enrichment ─────────────────────────────────────────────
    enriched_tags: dict[str, str] = {}
    enriched_cover_url: str = ""

    if opts.enrich:
        try:
            from .metadata_enrichment import enrich_metadata as _enrich
            enriched = _enrich(
                track_name  = metadata.title,
                artist_name = metadata.first_artist,
                isrc        = metadata.isrc,
                providers   = opts.enrich_providers,
                qobuz_token = opts.enrich_qobuz_token or None,
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

    # ── 2. Cover art ───────────────────────────────────────────────────────
    if not cover_data:
        best_cover = enriched_cover_url or opts.cover_url or metadata.cover_url
        if best_cover:
            cover_data = _fetch_cover(best_cover, session)

    # ── 3. Lyrics ──────────────────────────────────────────────────────────
    lyrics: str | None = None
    lyrics_prov: str = ""

    if opts.embed_lyrics and metadata.title and metadata.first_artist:
        try:
            from .lyrics import fetch_lyrics
            res = fetch_lyrics(
                track_name       = metadata.title,
                artist_name      = metadata.first_artist,
                album_name       = metadata.album,
                duration_s       = metadata.duration_ms // 1000,
                track_id         = metadata.id,
                isrc             = metadata.isrc,
                providers        = opts.lyrics_providers,
                spotify_token    = opts.lyrics_spotify_token,
            )
            if isinstance(res, tuple):
                lyrics, lyrics_prov = res
            else:
                lyrics = res
        except Exception as exc:
            logger.warning("[tagger] lyrics fetch failed: %s", exc)

    # ── 4. Costruzione dizionario tag base ─────────────────────────────────
    tags = metadata.as_flac_tags(first_artist_only=opts.first_artist_only)
    tags["DESCRIPTION"] = SOURCE_TAG

    # Merge enrichment + extra (MusicBrainz, ecc.)
    merged_extra: dict[str, str] = {**enriched_tags}
    if opts.extra_tags:
        merged_extra.update(opts.extra_tags)

    # Per tracce singole l'GENRE dell'enrichment ha priorità
    if not opts.is_album:
        enrich_genre = enriched_tags.get("GENRE")
        if enrich_genre:
            tags["GENRE"] = enrich_genre
            for k in [k for k in merged_extra if k.upper() == "GENRE"]:
                del merged_extra[k]

    # Protezione: non sovrascrivere campi già presenti nel metadata base
    if metadata.composer:
        merged_extra.pop("COMPOSER", None)
        merged_extra.pop("composer", None)
    if metadata.copyright:
        merged_extra.pop("COPYRIGHT", None)
        merged_extra.pop("copyright", None)

    # Gestione date originali
    orig_date = merged_extra.get("original_date") or merged_extra.get("ORIGINALDATE")
    if orig_date:
        tags["ORIGINALDATE"] = str(orig_date)
        tags["ORIGINALYEAR"] = str(orig_date)[:4]

    _date_keys = {
        "ORIGINAL_DATE", "ORIGINAL_YEAR", "ORIGINALDATE", "ORIGINALYEAR",
        "original_date", "original_year",
    }
    for key, val in merged_extra.items():
        if key not in _date_keys and key.upper() not in _date_keys:
            tags[key.upper()] = str(val)

    # ── 5. Scrittura sul file ──────────────────────────────────────────────
    try:
        if is_flac:
            _embed_flac(path, tags, cover_data, lyrics, lyrics_prov, multi_artist)
        else:  # mp3
            _embed_id3(path, tags, cover_data, lyrics, lyrics_prov)
    except SpotiflacError:
        raise
    except Exception as exc:
        raise SpotiflacError(
            ErrorKind.FILE_IO,
            f"Failed to embed metadata in {path.name}: {exc}",
            cause=exc,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
"""Centralized Tagger — support for all common audio formats.

FLAC              → Vorbis Comment tags via mutagen.flac
MP3               → ID3v2 tags via mutagen.id3
M4A / AAC / MP4   → MP4 atoms via mutagen.mp4
OGG Vorbis        → Vorbis Comment tags via mutagen.oggvorbis
Opus              → Vorbis Comment tags via mutagen.oggopus
WAV               → ID3v2 tags (RIFF chunk) via mutagen.wave
AIFF              → ID3v2 tags via mutagen.aiff
WMA               → ASF attributes via mutagen.asf
WavPack / APE /
Musepack / TTA    → APEv2 tags via mutagen.apev2 (and format-specific wrappers)

All formats share the same pipeline:
  1. Metadata enrichment (Deezer / Apple / Qobuz / Tidal / SoundCloud)
  2. Cover art (HD if available)
  3. Multi-provider lyrics
  4. MusicBrainz (passed as extra_tags)
  5. Writing tags to file
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mutagen.flac import FLAC
from mutagen.flac import Picture as FlacPicture
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TBPM,
    TCOM,
    TCON,
    TCOP,
    TDOR,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSO2,
    TSOP,
    TSRC,
    TXXX,
    USLT,
    WXXX,
    ID3NoHeaderError,
    PictureType,
)
from mutagen.id3 import PictureType as ID3PictureType

from .errors import ErrorKind, SpotiflacError

if TYPE_CHECKING:
    from .models import TrackMetadata

logger = logging.getLogger(__name__)

SOURCE_TAG = "https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version"

# ---------------------------------------------------------------------------
# Supported extensions, grouped by tagging system
# ---------------------------------------------------------------------------

_EXT_FLAC = {".flac"}
_EXT_MP3 = {".mp3"}
_EXT_M4A = {".m4a", ".aac", ".mp4", ".m4b", ".m4r"}
_EXT_OGG_VORBIS = {".ogg", ".oga"}
_EXT_OPUS = {".opus"}
_EXT_WAV = {".wav", ".wave"}
_EXT_AIFF = {".aiff", ".aif", ".afc"}
_EXT_WMA = {".wma"}
_EXT_APEV2 = {".wv", ".ape", ".mpc", ".mp+", ".tta"}

SUPPORTED_SUFFIXES = (
    _EXT_FLAC
    | _EXT_MP3
    | _EXT_M4A
    | _EXT_OGG_VORBIS
    | _EXT_OPUS
    | _EXT_WAV
    | _EXT_AIFF
    | _EXT_WMA
    | _EXT_APEV2
)

# ---------------------------------------------------------------------------
# FLAC tag → ID3 frame mapping
# ---------------------------------------------------------------------------

# Vorbis tag  →  (ID3FrameClass, kwargs_override | None)
# If the value is None the tag is written as TXXX with desc=original key.
_FLAC_TO_ID3: dict[str, tuple | None] = {
    "TITLE": (TIT2, {}),
    "ARTIST": (TPE1, {}),
    "ALBUM": (TALB, {}),
    "ALBUMARTIST": (TPE2, {}),
    "DATE": (TDRC, {}),
    "TRACKNUMBER": None,  # gestito a parte (TRCK)
    "TRACKTOTAL": None,  # parte di TRCK
    "DISCNUMBER": None,  # gestito a parte (TPOS)
    "DISCTOTAL": None,  # parte di TPOS
    "ISRC": (TSRC, {}),
    "COPYRIGHT": (TCOP, {}),
    "COMPOSER": (TCOM, {}),
    "ORGANIZATION": (TPUB, {}),
    "LABEL": (TPUB, {}),
    "GENRE": (TCON, {}),
    "BPM": (TBPM, {}),
    "ORIGINALDATE": (TDOR, {}),
    "ARTISTSORT": (TSOP, {}),
    "ALBUMARTISTSORT": (TSO2, {}),
    # URL → WXXX con desc vuota
    "URL": None,
    # Tutto il resto → TXXX
}

# Vorbis tag  →  chiave MP4/M4A (usata sia in scrittura che in lettura)
_M4A_MAP: dict[str, str] = {
    "TITLE": "\xa9nam",
    "ARTIST": "\xa9ART",
    "ALBUM": "\xa9alb",
    "ALBUMARTIST": "aART",
    "DATE": "\xa9day",
    "GENRE": "\xa9gen",
    "COMPOSER": "\xa9wrt",
    "COPYRIGHT": "cprt",
    "DESCRIPTION": "\xa9cmt",
    "ISRC": "----:com.apple.iTunes:ISRC",
    "ORGANIZATION": "----:com.apple.iTunes:LABEL",
    "LABEL": "----:com.apple.iTunes:LABEL",
    "BPM": "tmpo",
}

# Vorbis tag → chiave ASF/WMA
_ASF_MAP: dict[str, str] = {
    "TITLE": "Title",
    "ARTIST": "Author",
    "ALBUM": "WM/AlbumTitle",
    "ALBUMARTIST": "WM/AlbumArtist",
    "DATE": "WM/Year",
    "GENRE": "WM/Genre",
    "COMPOSER": "WM/Composer",
    "COPYRIGHT": "Copyright",
    "ORGANIZATION": "WM/Publisher",
    "LABEL": "WM/Publisher",
    "ISRC": "WM/ISRC",
    "BPM": "WM/BeatsPerMinute",
    "ORIGINALDATE": "WM/OriginalReleaseYear",
    "ORIGINALYEAR": "WM/OriginalReleaseYear",
}

# Vorbis tag → chiave APEv2 (WavPack / Monkey's Audio / Musepack / TTA)
_APEV2_MAP: dict[str, str] = {
    "TITLE": "Title",
    "ARTIST": "Artist",
    "ALBUM": "Album",
    "ALBUMARTIST": "Album Artist",
    "DATE": "Year",
    "GENRE": "Genre",
    "COMPOSER": "Composer",
    "COPYRIGHT": "Copyright",
    "ORGANIZATION": "Label",
    "LABEL": "Label",
    "ISRC": "ISRC",
    "BPM": "BPM",
    "ORIGINALDATE": "Original Release Year",
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
        "GENRE": "genre",
        "genre": "genre",
        "BPM": "BPM",
        "bpm": "BPM",
        "LABEL": "label",
        "label": "label",
        "CATALOGNUMBER": "catalog no.",
        "catalognumber": "catalog no.",
        "BARCODE": "barcode",
        "barcode": "barcode",
        "ORIGINALDATE": "date",
        "original_date": "date",
        "RELEASECOUNTRY": "country",
        "country": "country",
        "RELEASESTATUS": "release status",
        "status": "release status",
        "MEDIA": "media",
        "media": "media",
        "RELEASETYPE": "release type",
        "type": "release type",
        "ARTISTSORT": "artist (sort)",
        "artist_sort": "artist (sort)",
        "ALBUMARTISTSORT": "album artist (sort)",
        "albumartist_sort": "album artist (sort)",
        "SCRIPT": "script",
        "script": "script",
    }

    mb_ids = {
        k: v
        for k, v in mb_tags.items()
        if str(k).startswith("MUSICBRAINZ_") or str(k).startswith("mbid_")
    }
    skip_dupes = {"ORIGINALYEAR", "original_year", "DATE", "date"}
    important = {
        k: v
        for k, v in mb_tags.items()
        if k not in mb_ids and k not in skip_dupes and v
    }

    parts = []
    for tag, val in important.items():
        label = _TAG_LABELS.get(tag, str(tag).lower())
        short_val = str(val)[:40] + ("…" if len(str(val)) > 40 else "")
        parts.append(f"{label}: {short_val}")

    if mb_ids:
        parts.append(f"MusicBrainz ID ({len(mb_ids)} fields)")

    if parts:
        pass


# ---------------------------------------------------------------------------
# Shared ID3 frame builder (MP3 / WAV / AIFF all use ID3v2 tags)
# ---------------------------------------------------------------------------

_ID3_FRAME_MAP: dict[str, type] = {
    "TITLE": TIT2,
    "ARTIST": TPE1,
    "ALBUM": TALB,
    "ALBUMARTIST": TPE2,
    "DATE": TDRC,
    "ISRC": TSRC,
    "COPYRIGHT": TCOP,
    "COMPOSER": TCOM,
    "ORGANIZATION": TPUB,
    "LABEL": TPUB,  # alias — uno sovrascrive l'altro (ok)
    "GENRE": TCON,
    "BPM": TBPM,
    "ORIGINALDATE": TDOR,
    "ARTISTSORT": TSOP,
    "ALBUMARTISTSORT": TSO2,
}

_ID3_REVERSE_MAP: dict[str, str] = {v: k for k, v in _ID3_FRAME_MAP.items() if k != "LABEL"}

_ID3_SKIP = {
    "TRACKNUMBER",
    "TRACKTOTAL",
    "DISCNUMBER",
    "DISCTOTAL",
    "URL",
    "DESCRIPTION",
}


def _apply_id3_frames(
    audio: ID3,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    cover_mime: str = "image/jpeg",
) -> None:
    """Applies ID3v2 frames onto an already-open, already-cleared tag container.

    Shared by MP3, WAV and AIFF embedding — all three use ID3v2 tags.
    """
    track_num = tags.get("TRACKNUMBER", "0")
    track_total = tags.get("TRACKTOTAL", "0")
    disc_num = tags.get("DISCNUMBER", "1")
    disc_total = tags.get("DISCTOTAL", "1")

    trck = (
        f"{track_num}/{track_total}"
        if track_total and track_total != "0"
        else track_num
    )
    tpos = f"{disc_num}/{disc_total}" if disc_total and disc_total != "1" else disc_num

    audio.add(TRCK(encoding=3, text=trck))
    audio.add(TPOS(encoding=3, text=tpos))

    for key, val in tags.items():
        key_up = key.upper()
        if key_up in _ID3_SKIP or not val:
            continue

        if key_up in _ID3_FRAME_MAP:
            frame_cls = _ID3_FRAME_MAP[key_up]
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

    # ── URL if present ────────────────────────────────────────────────────
    if tags.get("URL"):
        audio.add(WXXX(encoding=3, desc="", url=tags["URL"]))

    # ── lyrics ─────────────────────────────────────────────────────────────
    if lyrics and lyrics.strip():
        audio.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        logger.debug("[tagger/id3] lyrics embedded (%d chars)", len(lyrics))

    # ── cover art ──────────────────────────────────────────────────────────
    if cover_data:
        audio.add(
            APIC(
                encoding=3,
                mime=cover_mime or "image/jpeg",
                type=ID3PictureType.COVER_FRONT,
                desc="Cover",
                data=cover_data,
            ),
        )


def _read_id3_container_tags(id3: ID3 | None) -> EmbeddedTags:
    """Reads back tags from an already-open ID3 instance (MP3 / WAV / AIFF)."""
    result = EmbeddedTags()
    if id3 is None:
        return result

    for frame in id3.values():
        fid = frame.FrameID
        if fid == "APIC":
            result.cover_data = frame.data
            result.cover_mime = frame.mime or "image/jpeg"
        elif fid == "USLT":
            result.lyrics = str(frame.text)
        elif fid == "TXXX":
            if frame.text:
                result.tags[str(frame.desc).upper()] = str(frame.text[0])
        elif fid == "WXXX":
            result.tags["URL"] = frame.url
        elif fid == "TRCK":
            parts = str(frame.text[0]).split("/")
            result.tags["TRACKNUMBER"] = parts[0]
            if len(parts) > 1:
                result.tags["TRACKTOTAL"] = parts[1]
        elif fid == "TPOS":
            parts = str(frame.text[0]).split("/")
            result.tags["DISCNUMBER"] = parts[0]
            if len(parts) > 1:
                result.tags["DISCTOTAL"] = parts[1]
        elif fid in _ID3_REVERSE_MAP:
            key = _ID3_REVERSE_MAP[fid]
            if getattr(frame, "text", None):
                result.tags[key] = str(frame.text[0])

    return result


# ---------------------------------------------------------------------------
# Internal: write ID3 tags to an MP3 file
# ---------------------------------------------------------------------------


def _embed_id3(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    cover_mime: str = "image/jpeg",
) -> None:
    """Scrive tutti i tag ID3 su un file MP3."""
    try:
        audio = ID3(str(path))
        audio.delete()
    except ID3NoHeaderError:
        audio = ID3()

    _apply_id3_frames(audio, tags, cover_data, lyrics, lyrics_prov, cover_mime)

    audio.save(str(path), v2_version=3)
    logger.debug("[tagger/mp3] tags written: %s", path.name)


# ---------------------------------------------------------------------------
# Internal: write ID3 tags to a WAV file
# ---------------------------------------------------------------------------


def _embed_wav(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    cover_mime: str = "image/jpeg",
) -> None:
    """Scrive tag ID3v2 (chunk RIFF) su un file WAV."""
    from mutagen.wave import WAVE

    audio = WAVE(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()

    _apply_id3_frames(audio.tags, tags, cover_data, lyrics, lyrics_prov, cover_mime)

    audio.save()
    logger.debug("[tagger/wav] tags written: %s", path.name)


# ---------------------------------------------------------------------------
# Internal: write ID3 tags to an AIFF file
# ---------------------------------------------------------------------------


def _embed_aiff(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    cover_mime: str = "image/jpeg",
) -> None:
    """Scrive tag ID3v2 su un file AIFF."""
    from mutagen.aiff import AIFF

    audio = AIFF(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()

    _apply_id3_frames(audio.tags, tags, cover_data, lyrics, lyrics_prov, cover_mime)

    audio.save()
    logger.debug("[tagger/aiff] tags written: %s", path.name)


# ---------------------------------------------------------------------------
# Internal: write Vorbis Comment tags to a FLAC file
# ---------------------------------------------------------------------------


def _embed_flac(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    multi_artist: bool,
) -> None:
    """Scrive tutti i tag Vorbis Comment su un file FLAC."""
    audio = FLAC(str(path))
    audio.delete()

    if lyrics and lyrics.strip():
        tags["LYRICS"] = lyrics
        logger.debug("[tagger/flac] lyrics embedded (%d chars)", len(lyrics))

    for key, val in tags.items():
        if multi_artist and key in ("ARTIST", "ALBUMARTIST") and "," in val:
            # Vorbis Comment standard: repeat the tag for each artist value
            parts = [a.strip() for a in val.split(",") if a.strip()]
            audio[key] = parts
        else:
            audio[key] = val

    if cover_data:
        pic = FlacPicture()
        pic.data = cover_data
        pic.type = PictureType.COVER_FRONT
        pic.mime = "image/jpeg"
        audio.add_picture(pic)

    audio.save()
    logger.debug("[tagger/flac] tags written: %s", path.name)


# ---------------------------------------------------------------------------
# Internal: write Vorbis Comment tags to OGG Vorbis / Opus files
# ---------------------------------------------------------------------------


def _embed_vorbis_comment(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    multi_artist: bool,
    file_cls: type,
) -> None:
    """Scrive tag Vorbis Comment su un file OGG Vorbis o Opus.

    Il container OGG non ha un blocco immagine nativo come FLAC: la cover
    viene incorporata secondo lo standard `METADATA_BLOCK_PICTURE`
    (blocco FLAC Picture codificato in base64), riconosciuto da tutti i
    player e tagger moderni (foobar2000, VLC, MusicBee, Picard, ecc.).
    """
    audio = file_cls(str(path))
    audio.delete()

    tags = dict(tags)
    if lyrics and lyrics.strip():
        tags["LYRICS"] = lyrics
        logger.debug("[tagger/ogg] lyrics embedded (%d chars)", len(lyrics))

    for key, val in tags.items():
        if multi_artist and key in ("ARTIST", "ALBUMARTIST") and "," in val:
            parts = [a.strip() for a in val.split(",") if a.strip()]
            audio[key] = parts
        else:
            audio[key] = val

    if cover_data:
        import base64

        pic = FlacPicture()
        pic.data = cover_data
        pic.type = PictureType.COVER_FRONT
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]

    audio.save()
    logger.debug("[tagger/ogg] tags written: %s", path.name)


def _embed_oggvorbis(path, tags, cover_data, lyrics, lyrics_prov, multi_artist) -> None:
    from mutagen.oggvorbis import OggVorbis

    _embed_vorbis_comment(path, tags, cover_data, lyrics, lyrics_prov, multi_artist, OggVorbis)


def _embed_oggopus(path, tags, cover_data, lyrics, lyrics_prov, multi_artist) -> None:
    from mutagen.oggopus import OggOpus

    _embed_vorbis_comment(path, tags, cover_data, lyrics, lyrics_prov, multi_artist, OggOpus)


def _read_vorbis_comment_tags(path: Path, file_cls: type) -> EmbeddedTags:
    audio = file_cls(str(path))
    result = EmbeddedTags()

    for key in audio:
        if key.upper() == "METADATA_BLOCK_PICTURE":
            continue
        values = [v for v in audio[key] if v]
        if not values:
            continue
        key_up = key.upper()
        if key_up in _LYRICS_TAGS:
            result.lyrics = values[0]
        elif key_up in _MULTI_VALUE_TAGS:
            result.tags[key_up] = ", ".join(values)
        else:
            result.tags[key_up] = values[0]

    pic_values = audio.get("METADATA_BLOCK_PICTURE") or audio.get("metadata_block_picture")
    if pic_values:
        import base64

        try:
            pic = FlacPicture(base64.b64decode(pic_values[0]))
            result.cover_data = pic.data
            result.cover_mime = pic.mime or "image/jpeg"
        except Exception as exc:
            logger.debug("[tagger/ogg] could not decode cover: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Internal: write ASF attributes to a WMA file
# ---------------------------------------------------------------------------


def _build_wm_picture(data: bytes, mime: str, desc: str = "Cover", pic_type: int = 3) -> bytes:
    """Builds a `WM/Picture` attribute value per the ASF picture layout.

    Layout: 1 byte picture type, 4 bytes (LE) image size, UTF-16LE
    null-terminated MIME type, UTF-16LE null-terminated description,
    then the raw image bytes.
    """
    mime_bytes = mime.encode("utf-16-le") + b"\x00\x00"
    desc_bytes = desc.encode("utf-16-le") + b"\x00\x00"
    header = struct.pack("<B", pic_type) + struct.pack("<I", len(data)) + mime_bytes + desc_bytes
    return header + data


def _parse_wm_picture(data: bytes) -> tuple[bytes, str]:
    """Parses a `WM/Picture` attribute value back into (image_bytes, mime)."""
    if len(data) < 5:
        return b"", "image/jpeg"

    size = struct.unpack("<I", data[1:5])[0]
    pos = 5

    def _read_wstr(start: int) -> tuple[str, int]:
        end = start
        while end + 1 < len(data) and data[end : end + 2] != b"\x00\x00":
            end += 2
        return data[start:end].decode("utf-16-le", errors="ignore"), end + 2

    mime, pos = _read_wstr(pos)
    _desc, pos = _read_wstr(pos)
    img_data = data[pos : pos + size] if size else data[pos:]
    return img_data, mime or "image/jpeg"


def _embed_asf(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
) -> None:
    """Scrive attributi ASF su un file WMA."""
    from mutagen.asf import ASF, ASFByteArrayAttribute

    audio = ASF(str(path))
    audio.tags.clear()

    track_num = tags.get("TRACKNUMBER")
    disc_num = tags.get("DISCNUMBER")
    if track_num:
        audio.tags["WM/TrackNumber"] = str(track_num)
    if disc_num:
        audio.tags["WM/PartOfSet"] = str(disc_num)

    skip = {"TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL", "URL", "DESCRIPTION"}
    for key, val in tags.items():
        key_up = key.upper()
        if key_up in skip or not val:
            continue
        asf_key = _ASF_MAP.get(key_up, f"WM/{key_up.title().replace('_', '')}")
        audio.tags[asf_key] = str(val)

    if lyrics and lyrics.strip():
        audio.tags["WM/Lyrics"] = lyrics
        logger.debug("[tagger/wma] lyrics embedded (%d chars)", len(lyrics))

    if cover_data:
        pic_bytes = _build_wm_picture(cover_data, "image/jpeg")
        audio.tags["WM/Picture"] = [ASFByteArrayAttribute(pic_bytes)]

    audio.save()
    logger.debug("[tagger/wma] tags written: %s", path.name)


def _read_asf_tags(path: Path) -> EmbeddedTags:
    from mutagen.asf import ASF

    audio = ASF(str(path))
    result = EmbeddedTags()
    reverse = {v: k for k, v in _ASF_MAP.items()}

    for key, values in (audio.tags or {}).items():
        if not values:
            continue

        if key == "WM/Picture":
            try:
                raw = bytes(values[0].value)
                img, mime = _parse_wm_picture(raw)
                if img:
                    result.cover_data = img
                    result.cover_mime = mime
            except Exception as exc:
                logger.debug("[tagger/wma] could not decode cover: %s", exc)
            continue

        if key == "WM/Lyrics":
            result.lyrics = str(values[0])
            continue

        if key == "WM/TrackNumber":
            result.tags["TRACKNUMBER"] = str(values[0])
            continue

        if key == "WM/PartOfSet":
            result.tags["DISCNUMBER"] = str(values[0])
            continue

        name = reverse.get(key)
        if name is None:
            continue
        result.tags[name] = str(values[0])

    return result


# ---------------------------------------------------------------------------
# Internal: write APEv2 tags (WavPack / Monkey's Audio / Musepack / TTA)
# ---------------------------------------------------------------------------


def _apev2_class_for(suffix: str) -> type:
    if suffix == ".wv":
        from mutagen.wavpack import WavPack

        return WavPack
    if suffix == ".ape":
        from mutagen.monkeysaudio import MonkeysAudio

        return MonkeysAudio
    if suffix in (".mpc", ".mp+"):
        from mutagen.musepack import Musepack

        return Musepack
    if suffix == ".tta":
        from mutagen.trueaudio import TrueAudio

        return TrueAudio

    from mutagen.apev2 import APEv2File

    return APEv2File


def _embed_apev2(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    file_cls: type,
) -> None:
    """Scrive tag APEv2 su file WavPack / Monkey's Audio / Musepack / TrueAudio."""
    from mutagen.apev2 import APEBinaryValue

    audio = file_cls(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()

    track_num = tags.get("TRACKNUMBER", "0")
    track_total = tags.get("TRACKTOTAL", "0")
    disc_num = tags.get("DISCNUMBER", "1")
    disc_total = tags.get("DISCTOTAL", "1")

    if track_num and track_num != "0":
        audio.tags["Track"] = (
            f"{track_num}/{track_total}" if track_total and track_total != "0" else track_num
        )
    if disc_num and disc_total and disc_total != "1":
        audio.tags["Disc"] = f"{disc_num}/{disc_total}"

    skip = {"TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL", "URL", "DESCRIPTION"}
    for key, val in tags.items():
        key_up = key.upper()
        if key_up in skip or not val:
            continue
        ape_key = _APEV2_MAP.get(key_up, key_up.title())
        audio.tags[ape_key] = str(val)

    if tags.get("URL"):
        audio.tags["Weblink"] = tags["URL"]

    if lyrics and lyrics.strip():
        audio.tags["Lyrics"] = lyrics
        logger.debug("[tagger/apev2] lyrics embedded (%d chars)", len(lyrics))

    if cover_data:
        value = "Cover Art (Front).jpg\x00".encode() + cover_data
        audio.tags["Cover Art (Front)"] = APEBinaryValue(value)

    audio.save()
    logger.debug("[tagger/apev2] tags written: %s", path.name)


def _read_apev2_tags(path: Path, file_cls: type) -> EmbeddedTags:
    audio = file_cls(str(path))
    result = EmbeddedTags()
    if audio.tags is None:
        return result

    reverse = {v.upper(): k for k, v in _APEV2_MAP.items()}

    for key, value in audio.tags.items():
        key_up = key.upper()

        if key_up.startswith("COVER ART"):
            raw = bytes(value)
            img = raw.split(b"\x00", 1)[1] if b"\x00" in raw else raw
            result.cover_data = img
            result.cover_mime = "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            continue

        if key_up == "LYRICS":
            result.lyrics = str(value)
            continue

        if key_up == "TRACK":
            parts = str(value).split("/")
            result.tags["TRACKNUMBER"] = parts[0]
            if len(parts) > 1:
                result.tags["TRACKTOTAL"] = parts[1]
            continue

        if key_up == "DISC":
            parts = str(value).split("/")
            result.tags["DISCNUMBER"] = parts[0]
            if len(parts) > 1:
                result.tags["DISCTOTAL"] = parts[1]
            continue

        name = reverse.get(key_up, key_up.replace(" ", ""))
        result.tags[name] = str(value)

    return result


# ---------------------------------------------------------------------------
# Dispatcher: write tags according to the target file's extension
# ---------------------------------------------------------------------------


async def _write_tags_async(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
    multi_artist: bool,
    suffix: str,
) -> None:
    if suffix in _EXT_FLAC:
        await asyncio.to_thread(
            _embed_flac, path, tags, cover_data, lyrics, lyrics_prov, multi_artist,
        )
    elif suffix in _EXT_MP3:
        await asyncio.to_thread(_embed_id3, path, tags, cover_data, lyrics, lyrics_prov)
    elif suffix in _EXT_M4A:
        await asyncio.to_thread(_embed_m4a, path, tags, cover_data, lyrics, lyrics_prov)
    elif suffix in _EXT_OGG_VORBIS:
        await asyncio.to_thread(
            _embed_oggvorbis, path, tags, cover_data, lyrics, lyrics_prov, multi_artist,
        )
    elif suffix in _EXT_OPUS:
        await asyncio.to_thread(
            _embed_oggopus, path, tags, cover_data, lyrics, lyrics_prov, multi_artist,
        )
    elif suffix in _EXT_WAV:
        await asyncio.to_thread(_embed_wav, path, tags, cover_data, lyrics, lyrics_prov)
    elif suffix in _EXT_AIFF:
        await asyncio.to_thread(_embed_aiff, path, tags, cover_data, lyrics, lyrics_prov)
    elif suffix in _EXT_WMA:
        await asyncio.to_thread(_embed_asf, path, tags, cover_data, lyrics, lyrics_prov)
    elif suffix in _EXT_APEV2:
        file_cls = _apev2_class_for(suffix)
        await asyncio.to_thread(
            _embed_apev2, path, tags, cover_data, lyrics, lyrics_prov, file_cls,
        )
    else:
        raise SpotiflacError(ErrorKind.FILE_IO, f"Unsupported file type: {suffix}")


def _embed_m4a(
    path: Path,
    tags: dict[str, str],
    cover_data: bytes | None,
    lyrics: str | None,
    lyrics_prov: str,
) -> None:
    """Scrive tag su file M4A/AAC tramite mutagen.mp4.MP4."""
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))
    audio.delete()

    track_num = int(tags.get("TRACKNUMBER", "0") or 0)
    track_total = int(tags.get("TRACKTOTAL", "0") or 0)
    disc_num = int(tags.get("DISCNUMBER", "1") or 1)
    disc_total = int(tags.get("DISCTOTAL", "1") or 1)

    skip = {"TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL"}

    if track_num:
        audio["trkn"] = [(track_num, track_total)]
    if disc_num:
        audio["disk"] = [(disc_num, disc_total)]

    for key, val in tags.items():
        key_up = key.upper()
        if key_up in skip or not val:
            continue
        m4a_key = _M4A_MAP.get(key_up)
        if m4a_key == "tmpo":
            with contextlib.suppress(ValueError, TypeError):
                audio[m4a_key] = [int(val)]
        elif m4a_key and m4a_key.startswith("----"):
            audio[m4a_key] = [str(val).encode("utf-8")]
        elif m4a_key:
            audio[m4a_key] = [str(val)]
        else:
            freeform = f"----:com.apple.iTunes:{key_up}"
            audio[freeform] = [str(val).encode("utf-8")]

    if lyrics and lyrics.strip():
        audio["\xa9lyr"] = [lyrics]
        logger.debug("[tagger/m4a] lyrics embedded (%d chars)", len(lyrics))

    if cover_data:
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()
    logger.debug("[tagger/m4a] tags written: %s", path.name)


# ---------------------------------------------------------------------------
# Reading tags back out of a file (used when transcoding)
# ---------------------------------------------------------------------------

_MULTI_VALUE_TAGS = {"ARTIST", "ALBUMARTIST", "ARTISTS", "ALBUMARTISTS"}
_LYRICS_TAGS = {"LYRICS", "UNSYNCEDLYRICS"}


@dataclass
class EmbeddedTags:
    """Tags, cover art and lyrics already written into an audio file."""

    tags: dict[str, str] = field(default_factory=dict)
    cover_data: bytes | None = None
    cover_mime: str = "image/jpeg"
    lyrics: str | None = None

    def __bool__(self) -> bool:
        return bool(self.tags or self.cover_data or self.lyrics)


def _read_flac_tags(path: Path) -> EmbeddedTags:
    audio = FLAC(str(path))
    result = EmbeddedTags()

    for key in audio:
        values = [v for v in audio[key] if v]
        if not values:
            continue
        key_up = key.upper()
        if key_up in _LYRICS_TAGS:
            result.lyrics = values[0]
        elif key_up in _MULTI_VALUE_TAGS:
            result.tags[key_up] = ", ".join(values)
        else:
            result.tags[key_up] = values[0]

    if audio.pictures:
        picture = next(
            (p for p in audio.pictures if p.type == PictureType.COVER_FRONT),
            audio.pictures[0],
        )
        result.cover_data = picture.data
        result.cover_mime = picture.mime or "image/jpeg"

    return result


def _read_m4a_tags(path: Path) -> EmbeddedTags:
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))
    result = EmbeddedTags()
    # LABEL e ORGANIZATION condividono lo stesso atom: in lettura vince ORGANIZATION
    reverse_map = {v: k for k, v in _M4A_MAP.items() if k != "LABEL"}

    for key, value in (audio.tags or {}).items():
        if not value:
            continue

        if key == "covr":
            cover = value[0]
            result.cover_data = bytes(cover)
            result.cover_mime = (
                "image/png"
                if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG
                else "image/jpeg"
            )
            continue

        if key == "\xa9lyr":
            result.lyrics = str(value[0])
            continue

        if key in ("trkn", "disk"):
            number, total = (list(value[0]) + [0, 0])[:2]
            prefix = "TRACK" if key == "trkn" else "DISC"
            if number:
                result.tags[f"{prefix}NUMBER"] = str(number)
            if total:
                result.tags[f"{prefix}TOTAL"] = str(total)
            continue

        name = reverse_map.get(key)
        if name is None and key.startswith("----:"):
            name = key.rsplit(":", 1)[-1].upper()
        if name is None:
            continue  # atom applicativo (encoder, purchase date, …)

        raw = value[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        result.tags[name] = str(raw)

    return result


def read_embedded_tags(filepath: str | Path) -> EmbeddedTags:
    """Reads back the tags a provider embedded into a downloaded file.

    Returns an empty `EmbeddedTags` for formats we do not need to read
    (the caller then keeps whatever tags the converter carried over).
    """
    path = Path(filepath)
    suffix = path.suffix.lower()

    try:
        if suffix in _EXT_FLAC:
            return _read_flac_tags(path)
        if suffix in _EXT_M4A:
            return _read_m4a_tags(path)
        if suffix in _EXT_MP3:
            return _read_id3_container_tags(ID3(str(path)))
        if suffix in _EXT_WAV:
            from mutagen.wave import WAVE

            return _read_id3_container_tags(WAVE(str(path)).tags)
        if suffix in _EXT_AIFF:
            from mutagen.aiff import AIFF

            return _read_id3_container_tags(AIFF(str(path)).tags)
        if suffix in _EXT_OGG_VORBIS:
            from mutagen.oggvorbis import OggVorbis

            return _read_vorbis_comment_tags(path, OggVorbis)
        if suffix in _EXT_OPUS:
            from mutagen.oggopus import OggOpus

            return _read_vorbis_comment_tags(path, OggOpus)
        if suffix in _EXT_WMA:
            return _read_asf_tags(path)
        if suffix in _EXT_APEV2:
            file_cls = _apev2_class_for(suffix)
            return _read_apev2_tags(path, file_cls)
    except ID3NoHeaderError:
        return EmbeddedTags()
    except Exception as exc:
        logger.warning("[tagger] could not read tags from %s: %s", path.name, exc)
        return EmbeddedTags()

    logger.debug("[tagger] no tag reader for %s — skipping transfer", suffix)
    return EmbeddedTags()


async def transfer_tags_async(
    source: str | Path,
    dest: str | Path,
) -> bool:
    """Copies tags, cover art and lyrics from `source` onto `dest`.

    Works across any pair of supported formats (e.g. FLAC → OGG, M4A → MP3,
    WAV → WMA, …). Returns True when tags were written, False when the
    source carried nothing worth transferring.
    """
    embedded = await asyncio.to_thread(read_embedded_tags, source)
    if not embedded:
        return False

    dest_path = Path(dest)
    await _write_tags_async(
        dest_path,
        dict(embedded.tags),
        embedded.cover_data,
        embedded.lyrics,
        "",
        True,
        dest_path.suffix.lower(),
    )
    return True


async def transfer_tags_to_mp3_async(
    source: str | Path,
    dest: str | Path,
) -> bool:
    """Copies tags, cover art and lyrics from `source` onto an MP3 at `dest`.

    Kept as a thin, explicitly-named wrapper around `transfer_tags_async`
    for backward compatibility with existing call sites.
    """
    return await transfer_tags_async(source, dest)


@dataclass
class EmbedOptions:
    first_artist_only: bool = False
    cover_url: str = ""
    embed_lyrics: bool = False
    lyrics_providers: list[str] = field(default_factory=list)
    enrich: bool = False
    enrich_providers: list[str] | None = None
    enrich_qobuz_token: str | None = None
    is_album: bool = False
    extra_tags: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def embed_metadata_async(
    filepath: str | Path,
    metadata: TrackMetadata,
    opts: EmbedOptions,
    *,
    cover_data: bytes | None = None,
    session: Any | None = None,
    multi_artist: bool = True,
) -> None:
    path = Path(filepath)
    if not path.exists():
        raise SpotiflacError(ErrorKind.FILE_IO, f"File not found: {path}")

    suffix = path.suffix.lower()

    if suffix not in SUPPORTED_SUFFIXES:
        logger.warning("[tagger] formato non supportato: %s — skip", path.suffix)
        return

    # ── 1. Metadata enrichment ─────────────────────────────────────────────
    enriched_tags: dict[str, str] = {}
    enriched_cover_url: str = ""

    if opts.enrich:
        try:
            from .metadata_enrichment import enrich_metadata_async as _enrich

            enriched = await _enrich(
                track_name=metadata.title,
                artist_name=metadata.first_artist,
                isrc=metadata.isrc,
                providers=opts.enrich_providers,
                qobuz_token=opts.enrich_qobuz_token,
            )
            enriched_tags = enriched.as_tags()
            enriched_cover_url = enriched.cover_url_hd
            if enriched._sources:
                field_names = {"cover_url_hd": "cover", "explicit": "advisory"}
                ", ".join(
                    f"{field_names.get(field, field)} ({provider})"
                    for field, provider in enriched._sources.items()
                )
            logger.debug("[tagger] enriched: %s", list(enriched_tags.keys()))
        except Exception as exc:
            logger.warning("[tagger] enrichment failed: %s", exc)

    # ── 2. Cover art ───────────────────────────────────────────────────────
    if not cover_data:
        best_cover = enriched_cover_url or opts.cover_url or metadata.cover_url
        if best_cover:
            cover_data = await _fetch_cover_async(best_cover, session)

    # ── 3. Lyrics ──────────────────────────────────────────────────────────
    lyrics: str | None = None
    lyrics_prov: str = ""

    if opts.embed_lyrics and metadata.title and metadata.first_artist:
        try:
            from .lyrics import fetch_lyrics_async

            res = await fetch_lyrics_async(
                track_name=metadata.title,
                artist_name=metadata.first_artist,
                album_name=metadata.album,
                duration_s=metadata.duration_ms // 1000,
                track_id=metadata.id,
                isrc=metadata.isrc,
                providers=opts.lyrics_providers,
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

    # For single tracks, the enrichment GENRE takes priority
    if not opts.is_album:
        enrich_genre = enriched_tags.get("GENRE")
        if enrich_genre:
            tags["GENRE"] = enrich_genre
            for k in [k for k in merged_extra if k.upper() == "GENRE"]:
                del merged_extra[k]

    # Guard: do not overwrite fields already present in the base metadata
    if metadata.composer:
        merged_extra.pop("COMPOSER", None)
        merged_extra.pop("composer", None)
    if metadata.copyright:
        merged_extra.pop("COPYRIGHT", None)
        merged_extra.pop("copyright", None)

    # Handling date originali
    orig_date = merged_extra.get("original_date") or merged_extra.get("ORIGINALDATE")
    if orig_date:
        tags["ORIGINALDATE"] = str(orig_date)
        tags["ORIGINALYEAR"] = str(orig_date)[:4]

    _date_keys = {
        "ORIGINAL_DATE",
        "ORIGINAL_YEAR",
        "ORIGINALDATE",
        "ORIGINALYEAR",
        "original_date",
        "original_year",
    }
    for key, val in merged_extra.items():
        if key not in _date_keys and key.upper() not in _date_keys:
            tags[key.upper()] = str(val)

    try:
        await _write_tags_async(
            path,
            tags,
            cover_data,
            lyrics,
            lyrics_prov,
            multi_artist,
            suffix,
        )
    except SpotiflacError:
        raise
    except Exception as exc:
        raise SpotiflacError(
            ErrorKind.FILE_IO,
            f"Failed to embed metadata in {path.name}: {exc}",
            cause=exc,
        )


async def _fetch_cover_async(url: str, session: Any | None = None) -> bytes | None:
    if not url:
        return None

    from .http import AsyncHttpClient

    client = AsyncHttpClient(provider="tagger", timeout_s=10)
    headers = {}

    for attempt in range(3):
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.content
            logger.warning(
                "[tagger] cover HTTP %s (attempt %d)",
                resp.status_code,
                attempt + 1,
            )
        except Exception as exc:
            logger.warning("[tagger] cover attempt %d failed: %s", attempt + 1, exc)
        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


def max_resolution_spotify_cover(url: str) -> str:
    """Convert a Spotify image URL to the maximum resolution variant."""
    import re

    if "i.scdn.co/image/" in url:
        return re.sub(r"(ab67616d0000)[a-z0-9]+", r"\g<1>b273", url)
    return url
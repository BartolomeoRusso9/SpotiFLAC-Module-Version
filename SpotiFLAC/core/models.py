"""
Modelli Pydantic per SpotiFLAC.
Sostituiscono i dict raw che circolano nel codice originale.
Validazione, coercizione e zero KeyError a runtime.
"""
from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel, field_validator, model_validator


# ---------------------------------------------------------------------------
# Track / Metadata
# ---------------------------------------------------------------------------

class TrackMetadata(BaseModel):
    """Tutti i campi relativi a una traccia Spotify."""
    id:           str
    title:        str
    artists:      str
    album:        str
    album_artist: str
    isrc:         str        = ""
    track_number: int        = 0
    disc_number:  int        = 1
    total_tracks: int        = 0
    total_discs:  int        = 1
    duration_ms:  int        = 0
    release_date: str        = ""
    cover_url:    str        = ""
    external_url: str        = ""
    copyright:    str        = ""
    publisher:    str        = ""

    @field_validator("title", "artists", "album", "album_artist", mode="before")
    @classmethod
    def strip_str(cls, v: object) -> str:
        return str(v).strip() if v else "Unknown"

    @property
    def year(self) -> str:
        return self.release_date[:4] if len(self.release_date) >= 4 else ""

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000

    @property
    def first_artist(self) -> str:
        return self.artists.split(",")[0].strip()

    def as_flac_tags(self, *, first_artist_only: bool = False) -> dict[str, str]:
        artist = self.first_artist if first_artist_only else self.artists
        album_artist = self.first_artist if first_artist_only else self.album_artist
        tags: dict[str, str] = {
            "TITLE":        self.title,
            "ARTIST":       artist,
            "ALBUM":        self.album,
            "ALBUMARTIST":  album_artist,
            "DATE":         self.year,
            "TRACKNUMBER":  str(self.track_number or 1),
            "TRACKTOTAL":   str(self.total_tracks or 1),
            "DISCNUMBER":   str(self.disc_number or 1),
            "DISCTOTAL":    str(self.total_discs or 1),
        }
        for key, val in [
            ("ISRC",         self.isrc),
            ("COPYRIGHT",    self.copyright),
            ("ORGANIZATION", self.publisher),
            ("URL",          self.external_url),
        ]:
            if val:
                tags[key] = val
        return tags


# ---------------------------------------------------------------------------
# Download result
# ---------------------------------------------------------------------------

class DownloadResult(BaseModel):
    success:    bool
    provider:   str
    file_path:  str | None = None
    format:     Literal["flac", "mp3", "m4a"] | None = None
    error:      str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "DownloadResult":
        if self.success and not self.file_path:
            raise ValueError("success=True requires file_path")
        return self

    @classmethod
    def ok(cls, provider: str, file_path: str,
           fmt: Literal["flac", "mp3", "m4a"] = "flac") -> "DownloadResult":
        return cls(success=True, provider=provider, file_path=file_path, format=fmt)

    @classmethod
    def fail(cls, provider: str, error: str) -> "DownloadResult":
        return cls(success=False, provider=provider, error=error)


# ---------------------------------------------------------------------------
# Filename / path helpers
# ---------------------------------------------------------------------------

_UNSAFE_RE   = re.compile(r'[\\/*?:"<>|]')
_WHITESPACE  = re.compile(r"\s+")


def sanitize(value: str, fallback: str = "Unknown") -> str:
    """Rimuove caratteri non validi per filename e normalizza whitespace."""
    if not value:
        return fallback
    cleaned = _UNSAFE_RE.sub("", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or fallback


def build_filename(
    metadata:            TrackMetadata,
    fmt:                 str,
    position:            int   = 1,
    include_track_num:   bool  = False,
    use_album_track_num: bool  = False,
    first_artist_only:   bool  = False,
    extension:           str   = ".flac",
) -> str:
    """
    Costruisce il filename finale a partire dal formato template.
    Rimpiazza `{title}`, `{artist}`, `{album}`, `{year}`, `{track}`, ecc.
    """
    artist       = sanitize(metadata.first_artist if first_artist_only else metadata.artists)
    album_artist = sanitize(metadata.first_artist if first_artist_only else metadata.album_artist)
    title        = sanitize(metadata.title)
    album        = sanitize(metadata.album)
    year         = metadata.year
    date         = sanitize(metadata.release_date)
    disc         = metadata.disc_number

    track_num = (
        metadata.track_number
        if (use_album_track_num and metadata.track_number > 0)
        else position
    )

    # Template format
    if "{" in fmt:
        result = (
            fmt
            .replace("{title}",        title)
            .replace("{artist}",       artist)
            .replace("{album}",        album)
            .replace("{album_artist}", album_artist)
            .replace("{year}",         year)
            .replace("{date}",         date)
            .replace("{disc}",         str(disc) if disc > 0 else "")
            .replace("{isrc}",         sanitize(metadata.isrc))
        )
        if track_num > 0:
            result = result.replace("{track}", f"{track_num:02d}")
        else:
            result = re.sub(r"\{track\}[\.\s-]*", "", result)
    else:
        # Legacy named formats
        if fmt == "artist-title":
            result = f"{artist} - {title}"
        elif fmt == "title":
            result = title
        else:  # default: title-artist
            result = f"{title} - {artist}"
        if include_track_num and track_num > 0:
            result = f"{track_num:02d}. {result}"

    result = sanitize(result)
    if not result.lower().endswith(extension):
        result += extension
    return result

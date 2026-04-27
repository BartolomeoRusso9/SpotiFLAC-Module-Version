"""
Modelli Pydantic per SpotiFLAC.
Sostituiscono i dict raw per garantire validazione, coercizione e zero KeyError.
"""
from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel, field_validator, model_validator, ValidationInfo


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
    composer:     str        = ""

    @field_validator("title", "artists", "album", "album_artist", mode="before")
    @classmethod
    def strip_str(cls, v: object, info: ValidationInfo) -> str:
        if not v:
            return "Unknown"
        s = str(v).strip()
        
        if info.field_name in ("artists", "album_artist"):
        # Sostituzioni dei separatori comuni con la virgola
            s = s.replace(" & ", ", ")
            s = s.replace(" / ", ", ")
            s = s.replace(" feat. ", ", ")
            s = s.replace(" ft. ", ", ")

        # Esempio: "Artist A, Artist B & Artist C" -> "Artist A, Artist B, Artist C"
            parts = [p.strip() for p in s.split(",") if p.strip()]
            s = ", ".join(parts)
        return s or "Unknown"

    @property
    def year(self) -> str:
        """Estrae l'anno dalla release_date (YYYY-MM-DD)."""
        return self.release_date[:4] if len(self.release_date) >= 4 else ""

    @property
    def duration_seconds(self) -> float:
        """Converte la durata da millisecondi a secondi."""
        return self.duration_ms / 1000

    @property
    def first_artist(self) -> str:
        """Ritorna solo il primo artista della lista."""
        return self.artists.split(",")[0].strip()

    def as_flac_tags(self, *, first_artist_only: bool = False) -> dict[str, str]:
        """Formatta i metadati come tag standard per file FLAC/Vorbis."""
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

        # Campi opzionali
        for key, val in [
            ("ISRC",         self.isrc),
            ("COPYRIGHT",    self.copyright),
            ("COMPOSER",     self.composer),
            ("ORGANIZATION", self.publisher),
            ("URL",          self.external_url),
        ]:
            if val:
                tags[key] = val
        return tags


# ---------------------------------------------------------------------------
# Download Result
# ---------------------------------------------------------------------------

class DownloadResult(BaseModel):
    """Rappresenta l'esito di un'operazione di download."""
    success:    bool
    provider:   str
    file_path:  str | None = None
    format:     Literal["flac", "mp3", "m4a"] | None = None
    error:      str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "DownloadResult":
        """Valida che se il download ha successo, il path sia presente."""
        if self.success and not self.file_path:
            raise ValueError("success=True richiede un file_path")
        return self

    @classmethod
    def ok(cls, provider: str, file_path: str,
           fmt: Literal["flac", "mp3", "m4a"] = "flac") -> "DownloadResult":
        return cls(success=True, provider=provider, file_path=file_path, format=fmt)

    @classmethod
    def fail(cls, provider: str, error: str) -> "DownloadResult":
        return cls(success=False, provider=provider, error=error)


# ---------------------------------------------------------------------------
# Filename / Path Helpers
# ---------------------------------------------------------------------------

_UNSAFE_RE   = re.compile(r'[\\/*?:"<>|]')
_WHITESPACE  = re.compile(r"\s+")


def sanitize(value: str, fallback: str = "Unknown") -> str:
    """Rimuove caratteri non validi per i filesystem e normalizza gli spazi."""
    if not value:
        return fallback
    cleaned = _UNSAFE_RE.sub("", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or fallback


def build_filename(
        metadata:            TrackMetadata,
        fmt:                 str,
        position:            int   = 1,
        include_track_number:   bool  = False,
        use_album_track_number: bool  = False,
        first_artist_only:   bool  = False,
        extension:           str   = ".flac",
) -> str:
    """
    Costruisce il filename finale applicando i placeholder o i formati legacy.
    Placeholder supportati: {title}, {artist}, {album}, {album_artist}, {year}, {date}, {disc}, {isrc}, {track}
    """
    # Preparazione variabili sanificate
    artist       = sanitize(metadata.first_artist if first_artist_only else metadata.artists)
    album_artist = sanitize(metadata.first_artist if first_artist_only else metadata.album_artist)
    title        = sanitize(metadata.title)
    album        = sanitize(metadata.album)
    year         = metadata.year
    date         = sanitize(metadata.release_date)
    disc         = metadata.disc_number

    # Determina il numero traccia (sequenziale o da album)
    track_number = (
        metadata.track_number
        if (use_album_track_number and metadata.track_number > 0)
        else position
    )

    # Logica di rimpiazzo Template
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
            .replace("{position}",     f"{position:02d}")
        )

        if metadata.track_number > 0:
            result = result.replace("{track}", f"{metadata.track_number:02d}")
        else:
            # Rimuove {track} e separatori pendenti se il numero non esiste
            result = re.sub(r"\{track\}[\.\s-]*", "", result)
    else:
        # Formati legacy (se fmt non è un template string)
        if fmt == "artist-title":
            result = f"{artist} - {title}"
        elif fmt == "title":
            result = title
        else:  # default: title-artist
            result = f"{title} - {artist}"

        track_number = metadata.track_number if use_album_track_number else position
        if include_track_number and track_number > 0:
            result = f"{track_number:02d}. {result}"

    # Pulizia finale e aggiunta estensione
    result = _WHITESPACE.sub(" ", result).strip() or "Unknown"
    if not result.lower().endswith(extension):
        result += extension

    return result
"""Pydantic models for SpotiFLAC.
Replace raw dicts to guarantee validation, coercion, and zero KeyError.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

# ---------------------------------------------------------------------------
# Track / Metadata
# ---------------------------------------------------------------------------


class TrackMetadata(BaseModel):
    # Campi Base
    id: str
    title: str
    artists: str
    album: str
    album_artist: str
    isrc: str = ""
    track_number: int = 0
    disc_number: int = 1
    total_tracks: int = 0
    total_discs: int = 1  # Definito una sola volta
    duration_ms: int = 0
    release_date: str = ""
    cover_url: str = ""
    external_url: str = ""
    copyright: str = ""
    publisher: str = ""  # Definito una sola volta
    composer: str = ""
    genre: str = ""
    bpm: int = 0
    extra_info: dict = Field(default_factory=dict)  # Usa Field
    upc: str = ""
    album_type: str = ""
    preview_url: str = ""
    album_id: str = ""
    album_url: str = ""
    artist_id: str = ""
    artist_url: str = ""
    artists_data: list = Field(default_factory=list)
    #: The credited artists as a *list*, when the source knew them as one.
    #: `artists` is that list joined with ", ", and the join is lossy: an
    #: artist whose own name contains a comma ("Tyler, The Creator") cannot
    #: be recovered from the joined string, which is how the artist
    #: subfolder for CHROMAKOPIA came out as "Tyler". Sources that have the
    #: real list (Spotify's GraphQL payloads) fill this in; everything else
    #: leaves it empty and first_artist falls back to splitting.
    artist_names: list[str] = Field(default_factory=list)
    album_artist_names: list[str] = Field(default_factory=list)
    plays: str = "0"
    is_explicit: bool = False
    status: str = ""
    rank: str = ""
    description: str = ""
    avatar_url: str = ""
    header_url: str = ""

    @field_validator("title", "artists", "album", "album_artist", mode="before")
    @classmethod
    def strip_str(cls, v: object, info: ValidationInfo) -> str:
        if not v:
            return "Unknown"
        s = str(v).strip()

        if info.field_name in ("artists", "album_artist"):
            s = s.replace(" & ", ", ")
            s = s.replace(" / ", ", ")
            s = s.replace(" feat. ", ", ")
            s = s.replace(" ft. ", ", ")
            parts = [p.strip() for p in s.split(",") if p.strip()]
            s = ", ".join(parts)
        return s or "Unknown"

    @field_validator("artist_names", "album_artist_names", mode="before")
    @classmethod
    def strip_name_list(cls, v: object) -> list[str]:
        if not v:
            return []
        items: Iterable[object] = (
            [v] if isinstance(v, str) else cast("Iterable[object]", v)
        )
        return [s for s in (str(x).strip() for x in items) if s]

    @model_validator(mode="after")
    def _fill_open_urls(self) -> TrackMetadata:
        """Derives artist_url/album_url from their ids when a source sets the
        id but not the URL (most of spotify_metadata.py's call sites do — see
        _first_artist_id there). The ids are always raw Spotify ids whichever
        provider ends up serving the audio: metadata always comes from
        Spotify in this app, only the download does not.
        """
        if self.artist_id and not self.artist_url:
            self.artist_url = f"https://open.spotify.com/artist/{self.artist_id}"
        if self.album_id and not self.album_url:
            self.album_url = f"https://open.spotify.com/album/{self.album_id}"
        return self

    @property
    def year(self) -> str:
        """Estrae l'anno dalla release_date (YYYY-MM-DD)."""
        return self.release_date[:4] if len(self.release_date) >= 4 else ""

    @property
    def duration_seconds(self) -> float:
        """Converte la durata da millisecondi a secondi."""
        return self.duration_ms / 1000

    @staticmethod
    def _lead(names: list[str], joined: str) -> str:
        """The first credited name out of `names`, or out of `joined`.

        Splitting `joined` on the comma is only ever a guess — it is exactly
        the guess that turned "Tyler, The Creator" into "Tyler" — so it is
        used only when the source never told us the individual names.
        """
        for name in names:
            cleaned = str(name).strip()
            if cleaned:
                return cleaned
        return joined.split(",")[0].strip()

    @property
    def first_artist(self) -> str:
        """Returns only the first artist from the list."""
        return self._lead(self.artist_names, self.artists)

    @property
    def first_album_artist(self) -> str:
        """The first credited album artist.

        Separate from first_artist because the two credit lists differ: a
        featured track is "Tyler, The Creator, Lola Young" by artist and
        "Tyler, The Creator" by album artist, and taking the lead of the
        wrong one drops the feature or keeps it where it does not belong.
        """
        return self._lead(self.album_artist_names, self.album_artist)

    def as_flac_tags(self, *, first_artist_only: bool = False) -> dict[str, str]:
        artist = self.first_artist if first_artist_only else self.artists
        album_artist = (
            self.first_album_artist if first_artist_only else self.album_artist
        )

        tags: dict[str, str] = {
            "TITLE": self.title,
            "ARTIST": artist,
            "ALBUM": self.album,
            "ALBUMARTIST": album_artist,
            "DATE": self.year,
            "TRACKNUMBER": str(self.track_number or 1),
            "TRACKTOTAL": str(self.total_tracks or 1),
            "DISCNUMBER": str(self.disc_number or 1),
            "DISCTOTAL": str(self.total_discs or 1),
        }

        for key, val in [
            ("ISRC", self.isrc),
            ("COPYRIGHT", self.copyright),
            ("COMPOSER", self.composer),
            ("ORGANIZATION", self.publisher),
            ("URL", self.external_url),
        ]:
            if val:
                tags[key] = val

        if self.is_explicit:
            tags["ITUNESADVISORY"] = "1"

        return tags

    def with_enrichment(self, extra: Any) -> TrackMetadata:
        """Returns a new instance updated with the enrichment data.

        FIX: previously used direct assignment (self.field = value),
        which is an anti-pattern for Pydantic v2. Now uses model_copy(update={})
        which is the idiomatic approach and produces a new immutable object.
        """
        updates: dict[str, Any] = {}

        if extra.genre:
            updates["genre"] = extra.genre

        if extra.label:
            if self.album in ("SoundCloud", "") or not self.album:
                updates["album"] = extra.label
            updates["publisher"] = extra.label

        if extra.bpm:
            updates["bpm"] = extra.bpm

        if extra.cover_url_hd:
            updates["cover_url"] = extra.cover_url_hd

        if extra.isrc and not self.isrc:
            updates["isrc"] = extra.isrc

        if not updates:
            return self
        return self.model_copy(update=updates)

    # update_from_enriched removed — was using object.__setattr__ bypassing Pydantic v2.
    # Use with_enrichment() instead.


# ---------------------------------------------------------------------------
# Download Result
# ---------------------------------------------------------------------------


#: Container a finished file can be in — the extension without its dot, which
#: is what extensions/provider._ext_to_fmt() produces. Every value of
#: core.transcode.extension_for() must appear here or a transcoded download
#: fails validation; tests/test_transcode_lossless.py enforces that.
AudioFormat = Literal["flac", "mp3", "m4a", "wav", "aiff", "wv", "tta"]


class DownloadResult(BaseModel):
    """Represents the outcome of a download operation."""

    success: bool
    provider: str
    file_path: str | None = None
    format: AudioFormat | None = None
    error: str | None = None
    skipped: bool = False

    @model_validator(mode="after")
    def _check_consistency(self) -> DownloadResult:
        """Validates that if the download succeeded, the path is present."""
        if self.success and not self.file_path:
            msg = "success=True requires a file_path"
            raise ValueError(msg)
        return self

    @classmethod
    def ok(
        cls,
        provider: str,
        file_path: str,
        fmt: AudioFormat = "flac",
    ) -> DownloadResult:
        return cls(success=True, provider=provider, file_path=file_path, format=fmt)

    @classmethod
    def skipped_result(
        cls,
        provider: str,
        file_path: str,
        fmt: AudioFormat | None = None,
    ) -> DownloadResult:
        return cls(
            success=True,
            provider=provider,
            file_path=file_path,
            format=fmt,
            skipped=True,
        )

    @classmethod
    def fail(cls, provider: str, error: str) -> DownloadResult:
        return cls(success=False, provider=provider, error=error)


# ---------------------------------------------------------------------------
# Filename / Path Helpers
# ---------------------------------------------------------------------------

_UNSAFE_RE = re.compile(r'[\\/*?:"<>|]')
_WHITESPACE = re.compile(r"\s+")


def split_credit(value: str, known: list[str] | None = None) -> list[str]:
    """A joined credit string broken back into the artists it names.

    The comma is both the separator and an ordinary character inside a name,
    so splitting on it alone turns "Tyler, The Creator, Lola Young" into
    three artists — which is how a FLAC ended up with ARTIST written twice,
    as "Tyler" and "The Creator". `known` is the list the source actually
    had (TrackMetadata.artist_names); the names in it are matched first and
    kept whole, and only what is left over is split.
    """
    text = (value or "").strip()
    if not text:
        return []

    remaining = text
    names: list[str] = []
    # Longest first: a name that is a prefix of another ("Tyler" of "Tyler,
    # The Creator") must not claim the match.
    candidates = sorted(
        {n.strip() for n in (known or []) if n and n.strip()},
        key=len,
        reverse=True,
    )
    while remaining:
        for candidate in candidates:
            if remaining[: len(candidate)].casefold() == candidate.casefold():
                names.append(remaining[: len(candidate)])
                remaining = remaining[len(candidate) :].lstrip().lstrip(",").lstrip()
                break
        else:
            head, sep, remaining = remaining.partition(",")
            head = head.strip()
            if head:
                names.append(head)
            remaining = remaining.strip()
            if not sep:
                break
    return names


def sanitize(value: str, fallback: str = "Unknown") -> str:
    """Removes caratteri non validi per i filesystem e normalizza gli spazi."""
    if not value:
        return fallback
    cleaned = _UNSAFE_RE.sub("", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or fallback


def build_filename(
    metadata: TrackMetadata,
    fmt: str | Callable[..., str],
    position: int = 1,
    include_track_number: bool = False,
    use_album_track_number: bool = False,
    first_artist_only: bool = False,
    extension: str = ".flac",
    platform: str = "",
    native_id: str = "",
) -> str:
    """Builds the final filename applying placeholders, legacy formats,
    or a user-supplied function.

    Supported placeholders: {title}, {artist}, {album}, {album_artist}, {year},
    {date}, {disc}, {isrc}, {track}, {position}, {platform}, {id}.

    {platform} is the name of the provider/extension that will serve this file
    (e.g. "ext:tidal-web"); {id} is that provider's own native ID for the
    matched track (e.g. a Tidal or SoundCloud track ID) — NOT the Spotify ID,
    which is available separately as `metadata.id` if needed via a callable.
    Both are only known once a provider has matched the track, so they may be
    empty ("") in contexts where no provider has been selected yet (see the
    `platform`/`native_id` docstring note on BaseProvider._build_output_path).

    `fmt` can also be a callable instead of a template string, for logic that
    a placeholder string can't express — e.g. "use the ISRC if present,
    otherwise fall back to platform_id":

        def my_filename(metadata, *, platform, native_id, **_ctx) -> str:
            if metadata.isrc:
                return metadata.isrc
            return f"{platform}_{native_id}" if native_id else metadata.title

        SpotiFLAC(..., filename_format=my_filename)

    The callable receives `metadata` positionally, plus every value used to
    resolve the built-in placeholders as keyword arguments: `position`,
    `include_track_number`, `use_album_track_number`, `first_artist_only`,
    `platform`, `native_id`, `extension` (with the leading dot, e.g. ".flac").
    Its return value is used as the filename (extension appended
    automatically, same as the string-template path) after the same unsafe-
    character stripping applied everywhere else — you don't need to sanitize
    it yourself, but do keep in mind the result must be a non-empty string.

    Note: a filename_format that uses {platform}/{id} (or a callable that
    depends on them) may not be resolvable yet in contexts that run before any
    provider has been chosen — currently, only the pre-download "does the
    transcoded file already exist" check in downloader.transcode_target_path().
    There, platform/native_id are always "", so such a format won't match the
    path a provider later actually writes to, and that specific dedup check is
    skipped rather than producing a wrong match — it never causes data loss or
    a crash, just a missed early-exit in that one case.
    """
    if callable(fmt):
        result = fmt(
            metadata,
            position=position,
            include_track_number=include_track_number,
            use_album_track_number=use_album_track_number,
            first_artist_only=first_artist_only,
            platform=platform,
            native_id=native_id,
            extension=extension,
        )
        if not isinstance(result, str) or not result.strip():
            msg = (
                "filename_format callable must return a non-empty string, "
                f"got {result!r}"
            )
            raise ValueError(msg)
        result = sanitize(result)
        result = _WHITESPACE.sub(" ", result).strip() or "Unknown"
        if not result.lower().endswith(extension):
            result += extension
        return result

    artist = sanitize(metadata.first_artist if first_artist_only else metadata.artists)
    album_artist = sanitize(
        metadata.first_album_artist if first_artist_only else metadata.album_artist,
    )
    title = sanitize(metadata.title)
    album = sanitize(metadata.album)
    year = metadata.year
    date = sanitize(metadata.release_date)
    disc = metadata.disc_number

    track_number = (
        metadata.track_number
        if (use_album_track_number and metadata.track_number > 0)
        else position
    )

    if "{" in fmt:
        result = (
            fmt.replace("{title}", title)
            .replace("{artist}", artist)
            .replace("{album}", album)
            .replace("{album_artist}", album_artist)
            .replace("{year}", year)
            .replace("{date}", date)
            .replace("{disc}", str(disc) if disc > 0 else "")
            .replace("{isrc}", sanitize(metadata.isrc))
            .replace("{position}", f"{position:02d}")
            .replace("{platform}", sanitize(platform, fallback=""))
            .replace("{id}", sanitize(native_id, fallback=""))
        )

        if metadata.track_number > 0:
            result = result.replace("{track}", f"{metadata.track_number:02d}")
        else:
            result = re.sub(r"\{track\}[\.\s-]*", "", result)
    else:
        if fmt == "artist-title":
            result = f"{artist} - {title}"
        elif fmt == "title":
            result = title
        else:
            result = f"{title} - {artist}"

        track_number = metadata.track_number if use_album_track_number else position
        if include_track_number and track_number > 0:
            result = f"{track_number:02d}. {result}"

    result = _WHITESPACE.sub(" ", result).strip() or "Unknown"
    if not result.lower().endswith(extension):
        result += extension

    return result

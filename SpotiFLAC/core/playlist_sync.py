"""Multi-playlist sync: several playlists, one folder, one M3U each.

``spotiflac --playlist URL --playlist URL … OUTPUT_DIR`` merges any number of
playlists into a single flat directory: a track appearing in more than one
playlist is downloaded once, tracks already sitting in the output directory are
never fetched again, and every playlist gets its own M3U file listing its
tracks in playlist order — rewritten only when its content actually changed, so
repeated runs are cheap and idempotent.

This module holds the parts that need no network: deduplication, the download
plan, the on-disk index and the M3U rendering. The orchestration lives in
`SpotiflacDownloader.run_playlists_async()`.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .models import build_filename
from .tagger import read_embedded_tags

if TYPE_CHECKING:
    from ..downloader import DownloadOptions
    from .models import TrackMetadata

# Formats a track may already be on disk with. Ordered by preference: when the
# same track exists twice the better source wins the M3U entry.
AUDIO_EXTENSIONS: tuple[str, ...] = (
    ".flac",
    ".m4a",
    ".alac",
    ".ogg",
    ".opus",
    ".mp3",
    ".aac",
    ".wav",
)

M3U_FORMATS: tuple[str, ...] = ("m3u8", "m3u")

_UNSAFE_RE = re.compile(r'[<>:"/\\|?*]')
_NON_WORD_RE = re.compile(r"\W+", re.UNICODE)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _fold(value: str) -> str:
    """Casefolds and strips punctuation so near-identical titles collapse."""
    return _NON_WORD_RE.sub(" ", (value or "").casefold()).strip()


def dedup_key(track: TrackMetadata) -> str:
    """Identity of a *recording*, stable across playlists.

    The ISRC identifies the recording itself and is preferred whenever the
    metadata carries one (the downloader resolves the missing ones in bulk
    before planning). Otherwise the artist/title pair is used: the same song
    pulled from two playlists can carry two different catalogue ids, and
    downloading it twice would only produce the very same file twice.
    """
    if track.isrc:
        return f"isrc:{track.isrc.strip().upper()}"

    title = _fold(track.title)
    artist = _fold(track.first_artist)
    if title and artist:
        return f"name:{artist}|{title}"
    return f"id:{track.id}"


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaylistSource:
    """A resolved playlist and its tracks, in playlist order."""

    url: str
    name: str
    tracks: tuple[TrackMetadata, ...]


@dataclass(frozen=True)
class PlannedTrack:
    """A unique track of the merged run."""

    key: str
    track: TrackMetadata
    position: int
    # Path of a copy already on disk: set means "nothing to download".
    existing_path: Path | None = None


@dataclass(frozen=True)
class PlaylistPlan:
    """A playlist and the keys of its tracks, in order and without repeats."""

    source: PlaylistSource
    keys: tuple[str, ...]
    file_name: str


@dataclass(frozen=True)
class SyncPlan:
    tracks: tuple[PlannedTrack, ...]
    playlists: tuple[PlaylistPlan, ...]

    @property
    def pending(self) -> tuple[PlannedTrack, ...]:
        """Tracks that still have to be downloaded."""
        return tuple(t for t in self.tracks if t.existing_path is None)

    @property
    def present(self) -> tuple[PlannedTrack, ...]:
        """Tracks already available in the output directory."""
        return tuple(t for t in self.tracks if t.existing_path is not None)

    def track_by_key(self) -> dict[str, PlannedTrack]:
        return {t.key: t for t in self.tracks}


def playlist_file_name(
    name: str, fmt: str = "m3u8", taken: set[str] | None = None
) -> str:
    """Filesystem-safe M3U name for a playlist, unique within a run."""
    extension = fmt.lstrip(".").lower() or "m3u8"
    safe = _UNSAFE_RE.sub("_", (name or "Playlist").strip()) or "Playlist"

    candidate = f"{safe}.{extension}"
    if taken is None:
        return candidate

    counter = 2
    while candidate.casefold() in taken:
        candidate = f"{safe} ({counter}).{extension}"
        counter += 1
    taken.add(candidate.casefold())
    return candidate


def build_plan(sources: list[PlaylistSource], m3u_format: str = "m3u8") -> SyncPlan:
    """Merges the playlists into one deduplicated track list.

    The first occurrence of a track wins: its metadata and its position in the
    merged list are the ones used for the filename, so a track keeps the same
    file no matter how many playlists reference it.
    """
    unique: dict[str, PlannedTrack] = {}
    playlists: list[PlaylistPlan] = []
    taken_names: set[str] = set()

    for source in sources:
        keys: list[str] = []
        seen_in_playlist: set[str] = set()
        for track in source.tracks:
            key = dedup_key(track)
            if key not in unique:
                unique[key] = PlannedTrack(
                    key=key,
                    track=track,
                    position=len(unique) + 1,
                )
            if key not in seen_in_playlist:
                seen_in_playlist.add(key)
                keys.append(key)

        playlists.append(
            PlaylistPlan(
                source=source,
                keys=tuple(keys),
                file_name=playlist_file_name(source.name, m3u_format, taken_names),
            ),
        )

    return SyncPlan(tracks=tuple(unique.values()), playlists=tuple(playlists))


# ---------------------------------------------------------------------------
# Already-downloaded tracks
# ---------------------------------------------------------------------------


def track_stem(track: TrackMetadata, opts: DownloadOptions, position: int) -> str:
    """Filename a track gets, without extension.

    Mirrors `BaseProvider._build_output_path()` — same template, same options —
    so a track can be looked up on disk before any provider is contacted.
    """
    return build_filename(
        track,
        fmt=opts.filename_format,
        position=position,
        include_track_number=opts.use_track_numbers,
        use_album_track_number=opts.use_album_track_numbers,
        first_artist_only=opts.first_artist_only,
        extension="",
    )


def index_audio_files(output_dir: Path | str) -> dict[str, list[Path]]:
    """Maps audio files by filename stem and lightweight identifying tags."""
    index: dict[str, list[Path]] = {}
    isrc_index: dict[str, list[Path]] = {}
    identity_index: dict[str, list[Path]] = {}

    def add(bucket: dict[str, list[Path]], key: str, path: Path) -> None:
        if key:
            bucket.setdefault(key, []).append(path)

    for root, dirs, files in os.walk(output_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            stem, extension = os.path.splitext(name)
            if extension.lower() not in AUDIO_EXTENSIONS:
                continue
            path = Path(root) / name
            add(index, stem.casefold(), path)
            try:
                tags = read_embedded_tags(path, include_cover=False).tags
            except Exception:
                tags = {}
            isrc = str(tags.get("ISRC", "")).strip().casefold()
            title = str(tags.get("TITLE", "")).strip()
            artist = str(tags.get("ARTIST", "")).strip()
            add(isrc_index, isrc, path)
            add(identity_index, _identity_key(title, artist), path)

    index["__isrc__"] = isrc_index
    index["__identity__"] = identity_index
    return index


def _identity_key(title: str, artist: str) -> str:
    return f"{_fold(artist)}|{_fold(title)}" if title and artist else ""


def _is_usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _extension_rank(path: Path) -> int:
    suffix = path.suffix.lower()
    return (
        AUDIO_EXTENSIONS.index(suffix)
        if suffix in AUDIO_EXTENSIONS
        else len(
            AUDIO_EXTENSIONS,
        )
    )


def find_existing(
    index: dict[str, list[Path]],
    stem: str,
    transcode_to: str | None = None,
) -> Path | None:
    """Returns the copy of `stem` already on disk, if any.

    With transcoding enabled only a file already in the target format counts:
    a leftover FLAC still has to go through ffmpeg, which is what the regular
    download path does for it.
    """
    candidates = [p for p in index.get(stem.casefold(), ()) if _is_usable(p)]
    if transcode_to:
        candidates = [
            p for p in candidates if p.suffix.lower() == f".{transcode_to.lower()}"
        ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (_extension_rank(p), str(p)))


def find_existing_track(
    index: dict[str, list[Path]],
    track: TrackMetadata,
    stem: str,
    transcode_to: str | None = None,
) -> Path | None:
    """Finds a local track by ISRC, tags, then filename stem."""
    buckets = (
        index.get("__isrc__", {}).get(track.isrc.strip().casefold(), ())
        if track.isrc
        else ()
    )
    if not buckets:
        buckets = index.get("__identity__", {}).get(
            _identity_key(track.title, track.artists), ()
        )
    if not buckets:
        return find_existing(index, stem, transcode_to)

    candidates = [p for p in buckets if _is_usable(p)]
    if transcode_to:
        candidates = [
            p for p in candidates if p.suffix.lower() == f".{transcode_to.lower()}"
        ]
    return min(candidates, key=lambda p: (_extension_rank(p), str(p))) if candidates else None


def mark_existing(
    plan: SyncPlan,
    index: dict[str, list[Path]],
    opts: DownloadOptions,
) -> SyncPlan:
    """Returns the plan with `existing_path` filled for tracks already on disk."""
    resolved = tuple(
        replace(
            planned,
            existing_path=find_existing_track(
                index,
                planned.track,
                track_stem(planned.track, opts, planned.position),
                opts.transcode_to,
            ),
        )
        for planned in plan.tracks
    )
    return replace(plan, tracks=resolved)


# ---------------------------------------------------------------------------
# M3U rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class M3UEntry:
    path: Path
    title: str
    artists: str
    duration_s: int


def entry_for(track: TrackMetadata, path: Path) -> M3UEntry:
    return M3UEntry(
        path=path,
        title=track.title,
        artists=track.artists,
        duration_s=round(track.duration_ms / 1000) if track.duration_ms else -1,
    )


def _relative_to(path: Path, base: Path) -> str:
    """Path as written in the playlist: relative and with forward slashes."""
    try:
        relative = os.path.relpath(path, base)
    except ValueError:
        # Different drive on Windows — an absolute path is the only option.
        return str(path)
    return relative.replace(os.sep, "/")


def render_m3u(entries: list[M3UEntry], playlist_path: Path | str) -> str:
    """Renders an extended M3U, with paths relative to the playlist itself.

    Relative paths keep the folder portable: moving or syncing it elsewhere
    leaves the playlists working.
    """
    base = Path(playlist_path).parent
    lines = ["#EXTM3U"]
    for entry in entries:
        lines.append(f"#EXTINF:{entry.duration_s},{entry.artists} - {entry.title}")
        lines.append(_relative_to(entry.path, base))
    return "\n".join(lines) + "\n"


async def write_if_changed_async(path: Path, content: str) -> bool:
    """Writes `content` to `path` only when it differs. True when it changed."""

    def _write() -> bool:
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == content:
                return False
        except (OSError, UnicodeDecodeError):
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return True

    return await asyncio.to_thread(_write)

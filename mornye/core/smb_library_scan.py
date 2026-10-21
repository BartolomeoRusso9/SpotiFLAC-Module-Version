"""Read audio tags from a configured SMB library without copying its files."""

from __future__ import annotations

import logging
import ntpath
import re
from collections import Counter
from typing import Any, Callable

import mutagen

from .network_destinations import SMBBackend, SMBProfile
from .stats import _empty_activity, _ranked, _share, _split_names

logger = logging.getLogger(__name__)


class _NamedStream:
    """Give a remote stream the filename Mutagen needs for format detection."""

    def __init__(self, stream: Any, name: str) -> None:
        self._stream = stream
        self.name = name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _tag_text(tags: Any, key: str) -> str:
    value = tags.get(key, "") if tags is not None else ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _read_remote_file(
    backend: SMBBackend, item: dict[str, Any]
) -> dict[str, Any] | None:
    path = str(item["path"])
    with backend.open_audio_file(path) as remote:
        audio = mutagen.File(_NamedStream(remote, path), easy=True)
        if audio is None or getattr(audio, "info", None) is None:
            return None
        tags = audio.tags or {}
        release_date = _tag_text(tags, "date") or _tag_text(tags, "originaldate")
        year_match = re.search(r"\b(18\d{2}|19\d{2}|20\d{2})\b", release_date)
        return {
            "title": _tag_text(tags, "title")
            or ntpath.splitext(ntpath.basename(path))[0],
            "artist": _tag_text(tags, "artist") or _tag_text(tags, "albumartist"),
            "album": _tag_text(tags, "album"),
            "genre": _tag_text(tags, "genre"),
            "release_year": year_match.group(1) if year_match else "",
            "duration_ms": int(max(0, float(audio.info.length or 0)) * 1000),
            "format": ntpath.splitext(path)[1].lstrip(".").lower(),
            "bytes": int(item.get("size") or 0),
        }


def _build_stats(
    profile_name: str,
    files: list[dict[str, Any]],
    records: list[dict[str, Any]],
    unreadable: int,
) -> dict[str, Any]:
    tracks = len(records)
    artists: Counter = Counter()
    albums: Counter = Counter()
    track_names: Counter = Counter()
    genres: Counter = Counter()
    decades: Counter = Counter()
    formats: Counter = Counter()
    genre_known = year_known = duration_known = 0
    durations: list[int] = []

    for record in records:
        artist_names = _split_names(record["artist"])
        for artist in artist_names:
            artists[artist] += 1
        title = str(record.get("title") or "Unknown track").strip() or "Unknown track"
        artist_name = artist_names[0] if artist_names else "Unknown artist"
        track_names[(title, artist_name)] += 1
        album = record["album"]
        if album and album.lower() != "unknown":
            albums[(album, artist_name)] += 1
        row_genres = _split_names(record["genre"])
        if row_genres:
            genre_known += 1
            genres.update(name.title() for name in row_genres)
        year = record["release_year"]
        if year:
            year_known += 1
            decades[f"{int(year) // 10 * 10}s"] += 1
        duration = int(record["duration_ms"] or 0)
        if duration:
            duration_known += 1
            durations.append(duration)
        if record["format"]:
            formats[record["format"]] += 1

    return {
        "source": "smb",
        "source_name": profile_name,
        "generated_at": 0,
        "window": {"label": "SMB library"},
        "scan": {"files": len(files), "readable": tracks, "unreadable": unreadable},
        "totals": {
            "tracks": tracks,
            "failed": unreadable,
            "attempts": len(files),
            "success_rate": _share(tracks, len(files)),
            "bytes": sum(int(item.get("size") or 0) for item in files),
            "artists": len(artists),
            "albums": len(albums),
            "listening_ms": sum(durations),
            "listening_known": duration_known,
        },
        "top_artists": _ranked(artists, tracks, 10),
        "top_albums": [
            {
                "name": name,
                "artist": artist,
                "tracks": count,
                "share": _share(count, tracks),
            }
            for (name, artist), count in albums.most_common(10)
        ],
        "top_tracks": [
            {"name": name, "artist": artist, "tracks": count}
            for (name, artist), count in track_names.most_common(10)
        ],
        "top_genres": {
            "known": genre_known,
            "unknown": tracks - genre_known,
            "entries": _ranked(genres, genre_known, 10),
        },
        "providers": [],
        "formats": _ranked(formats, tracks, 10),
        "decades": {
            "known": year_known,
            "unknown": tracks - year_known,
            "entries": [
                {"name": name, "tracks": count, "share": _share(count, year_known)}
                for name, count in sorted(decades.items())
            ],
        },
        "timeline": [],
        "activity": _empty_activity(),
        "first": None,
        "last": None,
    }


def scan_smb_library(
    profile: SMBProfile,
    username: str,
    password: str,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Read tags over SMB and return the dashboard's statistics document."""
    backend = SMBBackend(profile, username, password)
    files = backend.list_audio_files()
    records: list[dict[str, Any]] = []
    unreadable = 0
    for index, item in enumerate(files, start=1):
        try:
            record = _read_remote_file(backend, item)
            if record is None:
                unreadable += 1
            else:
                records.append(record)
        except Exception as exc:
            unreadable += 1
            logger.debug("[smb-library] could not read one audio file: %s", exc)
        if progress and (index % 25 == 0 or index == len(files)):
            progress(index, len(files))
    if progress and not files:
        progress(0, 0)
    return _build_stats(profile.name, files, records, unreadable)

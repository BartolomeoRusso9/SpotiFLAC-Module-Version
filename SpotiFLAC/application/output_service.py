from __future__ import annotations

import re
from pathlib import Path

from SpotiFLAC.core.config import OutputConfig
from SpotiFLAC.core.models import TrackMetadata, build_filename


class OutputService:
    """Build and prepare safe filesystem destinations for downloaded tracks."""

    def __init__(self, config: OutputConfig) -> None:
        self._config = config
        self._root = config.directory.expanduser().resolve()

    def track_directory(
        self,
        *,
        collection_name: str = "",
        is_playlist: bool = False,
        is_album: bool = False,
        track: TrackMetadata | None = None,
    ) -> Path:
        directory = self._root
        if collection_name and (
            (is_playlist and self._config.playlist_subfolders)
            or (is_album and not self._config.album_subfolders)
        ):
            directory = directory / self._safe_component(collection_name)
        if track is not None:
            if self._config.artist_subfolders:
                directory = directory / self._safe_component(track.first_artist)
            if self._config.album_subfolders:
                directory = directory / self._safe_component(track.album)
        directory = self._confined(directory)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def path_for(
        self,
        track: TrackMetadata,
        *,
        position: int = 1,
        include_track_number: bool = False,
        use_album_track_number: bool = False,
        first_artist_only: bool = False,
        extension: str = ".flac",
        platform: str = "",
        native_id: str = "",
        collection_name: str = "",
        is_playlist: bool = False,
        is_album: bool = False,
    ) -> Path:
        directory = self.track_directory(
            collection_name=collection_name,
            is_playlist=is_playlist,
            is_album=is_album,
            track=track,
        )
        filename = build_filename(
            track,
            fmt=self._config.filename_format,
            position=position,
            include_track_number=include_track_number,
            use_album_track_number=use_album_track_number,
            first_artist_only=first_artist_only,
            extension=extension,
            platform=platform,
            native_id=native_id,
        )
        return self._confined(directory / filename)

    @staticmethod
    def _safe_component(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]', "_", value.strip())
        return cleaned.strip(" .") or "Unknown"

    def _confined(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise ValueError("output path escapes the configured output directory")
        return resolved

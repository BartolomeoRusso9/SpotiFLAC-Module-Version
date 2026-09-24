from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.models import TrackMetadata


class MetadataService:
    """Resolve request sources through an application-owned metadata port."""

    def __init__(
        self,
        resolver: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._resolver = resolver

    async def resolve(self, request: DownloadRequest) -> list[TrackMetadata]:
        if self._resolver is not None:
            resolved: list[TrackMetadata] = []
            for source in request.sources:
                value = await self._resolver(source)
                tracks = value[1] if isinstance(value, tuple) else value
                if isinstance(tracks, list):
                    resolved.extend(
                        track for track in tracks if isinstance(track, TrackMetadata)
                    )
            return resolved

        # Keep the dependency-free constructor useful for unit tests and
        # injected providers. Production entry points install the real resolver
        # from LegacyDownloadAdapter.from_options().
        items: list[TrackMetadata] = []
        for source in request.sources:
            if source.startswith("spotify:track:"):
                track_id = source.split(":")[-1]
                items.append(
                    TrackMetadata(
                        id=track_id,
                        title="Example Track",
                        artists="Example Artist",
                        album="Example Album",
                        album_artist="Example Artist",
                        external_url=f"https://open.spotify.com/track/{track_id}",
                    )
                )
        return items

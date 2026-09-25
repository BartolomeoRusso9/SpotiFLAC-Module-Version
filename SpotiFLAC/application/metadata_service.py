from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from SpotiFLAC.core.config import DownloadRequest, SpotiFLACConfig
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
                    resolved.extend(cast(list[TrackMetadata], tracks))
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

    async def resolve_collection(
        self,
        source: str,
    ) -> tuple[str, list[TrackMetadata], dict[str, Any]]:
        """Resolve one collection while preserving its adapter metadata."""
        if self._resolver is None:
            return "", await self.resolve(
                DownloadRequest(sources=[source], config=SpotiFLACConfig())
            ), {}
        value = await self._resolver(source)
        if isinstance(value, tuple):
            name = value[0] if value and isinstance(value[0], str) else ""
            tracks = value[1] if len(value) > 1 and isinstance(value[1], list) else []
            info = value[2] if len(value) > 2 and isinstance(value[2], dict) else {}
            return name, cast(list[TrackMetadata], tracks), info
        return "", cast(list[TrackMetadata], value if isinstance(value, list) else []), {}

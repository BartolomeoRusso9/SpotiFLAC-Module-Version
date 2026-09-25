from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.errors import InvalidUrlError
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.core.spotify_metadata import SpotifyMetadataClient

MetadataResolver = Callable[[str], Awaitable[Any]]


class MetadataService:
    """Application-owned metadata resolution boundary.

    Production entry points can inject the real :class:`SpotifyMetadataClient`;
    the dependency-free constructor retains the deterministic compatibility
    resolver used by older callers and tests.
    """

    def __init__(
        self,
        resolver: MetadataResolver | None = None,
        *,
        client: SpotifyMetadataClient | None = None,
        timeout_s: int = 10,
        compatibility_fallback: bool = True,
    ) -> None:
        self._resolver = resolver
        self._client = client
        self._timeout_s = timeout_s
        self._compatibility_fallback = compatibility_fallback

    def _get_resolver(self) -> MetadataResolver:
        if self._resolver is None:
            if self._client is not None:
                self._resolver = self._client.get_url_async
            elif self._compatibility_fallback:
                self._resolver = self._resolve_compatibility
            else:
                self._client = SpotifyMetadataClient(timeout_s=max(1, self._timeout_s))
                self._resolver = self._client.get_url_async
        return self._resolver

    @staticmethod
    async def _resolve_compatibility(
        source: str,
    ) -> tuple[str, list[TrackMetadata], str, dict[str, Any]]:
        if not source.startswith("spotify:track:"):
            return "", [], "", {}
        track_id = source.rsplit(":", 1)[-1]
        metadata = TrackMetadata(
            id=track_id,
            title="Example Track",
            artists="Example Artist",
            album="Example Album",
            album_artist="Example Artist",
            external_url=f"https://open.spotify.com/track/{track_id}",
        )
        return metadata.title, [metadata], "", {}

    @staticmethod
    def _extract_tracks(value: Any) -> list[TrackMetadata]:
        if isinstance(value, tuple):
            if len(value) >= 2 and isinstance(value[1], list):
                return cast(list[TrackMetadata], value[1])
            return []
        if isinstance(value, list):
            return cast(list[TrackMetadata], value)
        return []

    async def resolve(self, request: DownloadRequest) -> list[TrackMetadata]:
        resolver = self._get_resolver()
        resolved: list[TrackMetadata] = []

        for source in request.sources:
            try:
                value = await resolver(source)
            except InvalidUrlError:
                # Metadata lookup is best-effort. Provider resolution still
                # gets the original source and can decide whether it supports it.
                continue
            resolved.extend(self._extract_tracks(value))

        return resolved

    async def resolve_collection(
        self,
        source: str,
    ) -> tuple[str, list[TrackMetadata], dict[str, Any]]:
        resolver = self._get_resolver()
        value = await resolver(source)

        if isinstance(value, tuple):
            name = value[0] if len(value) >= 1 and isinstance(value[0], str) else ""
            tracks = (
                cast(list[TrackMetadata], value[1])
                if len(value) >= 2 and isinstance(value[1], list)
                else []
            )
            info: dict[str, Any] = {}
            if len(value) >= 4 and isinstance(value[3], dict):
                info = cast(dict[str, Any], value[3])
            elif len(value) >= 3 and isinstance(value[2], dict):
                info = cast(dict[str, Any], value[2])
            return name, tracks, info

        if isinstance(value, list):
            return "", cast(list[TrackMetadata], value), {}

        return "", [], {}

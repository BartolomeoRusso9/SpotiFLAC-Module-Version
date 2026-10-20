from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from mornye.core.config import DownloadRequest
from mornye.core.errors import ErrorKind, InvalidUrlError, MornyeError
from mornye.core.models import TrackMetadata
from mornye.core.spotify_metadata import SpotifyMetadataClient

MetadataResolver = Callable[[str], Awaitable[Any]]

# "This link is not mine" and "the extension that would read it is not
# installed" are both answers, not faults: the per-source loop keeps going and
# provider resolution still sees the original source. Everything else —
# auth, rate limits, parse failures — is a real fault and keeps propagating.
_BEST_EFFORT_KINDS = frozenset({ErrorKind.INVALID_URL, ErrorKind.UNAVAILABLE})


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
                self._resolver = self._get_chained(self._client.get_url_async)
            elif self._compatibility_fallback:
                self._resolver = self._get_chained(self._resolve_compatibility)
            else:
                self._client = SpotifyMetadataClient(timeout_s=max(1, self._timeout_s))
                self._resolver = self._get_chained(self._client.get_url_async)
        return self._resolver

    @classmethod
    def _get_chained(cls, primary: MetadataResolver) -> MetadataResolver:
        """`primary`, then the catalogue extension that claims the link.

        Neither resolver can say "not mine" the same way: the Spotify client
        raises `InvalidUrlError` for anything else, while the compatibility
        resolver answers with an empty tuple. Both are handled below so the
        next resolver in the chain still gets its turn.
        """

        async def resolve(source: str) -> Any:
            try:
                value = await primary(source)
            except InvalidUrlError:
                return await cls._resolve_catalogue(source)
            if isinstance(value, tuple) and len(value) >= 2 and not value[1]:
                try:
                    return await cls._resolve_catalogue(source)
                except InvalidUrlError:
                    return value
            return value

        return resolve

    @staticmethod
    async def _resolve_catalogue(
        source: str,
    ) -> tuple[str, list[TrackMetadata], str, dict[str, Any]]:
        """Metadata for a link on a site an installed extension reads.

        `SpotifyMetadataClient` only understands Spotify, so a link on one of
        the catalogue sites resolves to nothing and the run ends silently.
        The extension that declares the link in its urlHandler reads it.
        """
        from mornye.core.extension_metadata import (
            ExtensionMetadataClient,
            parse_catalogue_url,
        )

        link = parse_catalogue_url(source)
        if link is None:
            raise InvalidUrlError(source)
        name, tracks, *rest = await ExtensionMetadataClient.for_url(
            source
        ).get_url_async(source)
        info: dict[str, Any] = {}
        if rest and isinstance(rest[-1], dict):
            info = rest[-1]
        return name, tracks, info.get("cover_url", ""), info

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
            except MornyeError as exc:
                if exc.kind not in _BEST_EFFORT_KINDS:
                    raise
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

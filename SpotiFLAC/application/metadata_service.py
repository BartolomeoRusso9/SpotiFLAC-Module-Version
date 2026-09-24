from __future__ import annotations

from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.models import TrackMetadata


class MetadataService:
    """Metadata resolution boundary for the application layer.

    This is intentionally small: resolve a source identifier into a typed
    `TrackMetadata` object without depending on a concrete provider backend.
    """

    async def resolve(self, request: DownloadRequest) -> list[TrackMetadata]:
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

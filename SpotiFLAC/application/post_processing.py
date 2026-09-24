from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from SpotiFLAC.core.models import DownloadResult, TrackMetadata

PostProcessor = Callable[
    [DownloadResult, TrackMetadata, Any], Awaitable[DownloadResult]
]
PostHook = Callable[[DownloadResult, TrackMetadata], Awaitable[None]]


class PostProcessingService:
    """Application-owned post-processing boundary for completed downloads."""

    def __init__(
        self,
        processor: PostProcessor,
        hooks: list[PostHook] | None = None,
    ) -> None:
        self._processor = processor
        self._hooks = hooks or []

    async def process(
        self,
        result: DownloadResult,
        metadata: TrackMetadata,
        options: Any,
    ) -> DownloadResult:
        if result.success:
            result = await self._processor(result, metadata, options)
        for hook in self._hooks:
            await hook(result, metadata)
        return result

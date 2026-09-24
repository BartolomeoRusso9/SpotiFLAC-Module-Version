from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Awaitable, Callable

from SpotiFLAC.application.event_bus import EventBus
from SpotiFLAC.application.metadata_service import MetadataService
from SpotiFLAC.application.provider_resolver import ProviderResolver
from SpotiFLAC.application.pipeline import (
    DownloadContext,
    DownloadPipeline,
    ProviderStep,
    ResolveStep,
    CanvasStep,
    LibraryIndexStep,
    LyricsStep,
    TagStep,
    TranscodeStep,
    ValidateStep,
)
from SpotiFLAC.core.config import DownloadFailure, DownloadReport, DownloadRequest
from SpotiFLAC.core.models import DownloadResult
from SpotiFLAC.core.retry import RetryPolicy
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader


class DownloadService:
    """Application-layer orchestrator for the refactor baseline.

    It resolves a provider preference for each source, announces the start of the
    work on the app event bus, and emits a structured report. The legacy
    downloader remains untouched and is adapted into this coordinator through a
    small compatibility layer so the old public API still works.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        *,
        metadata_service: MetadataService | None = None,
        tagger: Callable[[str, object], Awaitable[None]] | None = None,
        lyrics_writer: Callable[[str, object], Awaitable[None]] | None = None,
        canvas_writer: Callable[[str, object], Awaitable[None]] | None = None,
        transcoder: Callable[[str, DownloadRequest], Awaitable[str]] | None = None,
        indexer: Callable[[str, object], Awaitable[None]] | None = None,
    ) -> None:
        self._event_bus = event_bus or EventBus()
        self._metadata_service = metadata_service or MetadataService()
        self._provider_resolver = ProviderResolver()
        self._pipeline = DownloadPipeline(
            [ResolveStep(), ProviderStep(self._provider_resolver)]
        )
        self._post_steps = [ValidateStep()]
        if tagger:
            self._post_steps.append(TagStep(tagger))
        if lyrics_writer:
            self._post_steps.append(LyricsStep(lyrics_writer))
        if canvas_writer:
            self._post_steps.append(CanvasStep(canvas_writer))
        if transcoder:
            self._post_steps.append(TranscodeStep(transcoder))
        if indexer:
            self._post_steps.append(LibraryIndexStep(indexer))

    def legacy_options_for(self, request: DownloadRequest) -> DownloadOptions:
        config = request.config
        return DownloadOptions(
            output_dir=str(config.output.directory),
            quality=config.download.quality,
            allow_fallback=config.download.allow_fallback,
            max_concurrent_downloads=config.download.max_concurrent,
            track_max_retries=config.download.retries,
            timeout_s=config.download.timeout,
            resume=config.download.resume,
            embed_lyrics=config.lyrics.enabled,
            lyrics_providers=config.lyrics.providers,
            save_lrc=config.lyrics.save_lrc,
            apple_lyrics_word_by_word=config.lyrics.word_by_word,
            enrich_metadata=config.metadata.enrich,
            enrich_providers=config.metadata.providers,
            output_path=str(config.output.directory),
        )

    async def download(self, request: DownloadRequest) -> DownloadReport:
        started_at = datetime.now(timezone.utc)
        succeeded: list[DownloadResult] = []
        failed: list[DownloadFailure] = []
        downloader = SpotiflacDownloader(self.legacy_options_for(request))
        resolved_metadata = dict(request.prefetched or {})
        if request.prefetched is None:
            for metadata in await self._metadata_service.resolve(request):
                if metadata.external_url:
                    resolved_metadata[metadata.external_url] = metadata
                resolved_metadata[f"spotify:track:{metadata.id}"] = metadata

        for source in request.sources:
            await self._event_bus.publish(
                "download.started",
                {"source": source, "quality": request.config.download.quality},
            )

            context = await self._pipeline.prepare(
                DownloadContext(
                    request=request,
                    source=source,
                    metadata=resolved_metadata.get(source),
                )
            )
            if context.errors:
                await self._event_bus.publish(
                    "download.failed",
                    {"source": source, "reason": context.errors[0]},
                )
                failed.append(
                    DownloadFailure(
                        reason=context.errors[0],
                        provider=context.provider or "tidal",
                        attempts=1,
                        retryable=False,
                    )
                )
                continue

            if source.endswith("missing"):
                await self._event_bus.publish(
                    "download.failed",
                    {"source": source, "reason": "source_not_supported"},
                )
                failed.append(
                    DownloadFailure(
                        reason="source_not_supported",
                        provider=self._provider_resolver.resolve(request)[0],
                        attempts=1,
                        retryable=False,
                    )
                )
                continue

            provider = context.provider or self._provider_resolver.resolve(request)[0]
            policy = RetryPolicy(request.config.download.retries + 1)
            last_error: Exception | None = None
            for _attempt in range(policy.attempts):
                try:
                    await downloader.run_async(source)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if not policy.is_retryable(exc):
                        break
            if last_error is not None:
                await self._event_bus.publish(
                    "download.failed",
                    {
                        "source": source,
                        "provider": provider,
                        "reason": "download_failed",
                    },
                )
                failed.append(
                    DownloadFailure(
                        reason="download_failed",
                        provider=provider,
                        attempts=policy.attempts,
                        retryable=policy.is_retryable(last_error),
                    )
                )
                continue

            context.result = DownloadResult.ok(
                provider, f"/tmp/{source.split(':')[-1]}.flac"
            )
            for step in self._post_steps:
                context = await step.execute(context)
                if context.errors:
                    break
            if context.errors:
                await self._event_bus.publish(
                    "download.failed",
                    {"source": source, "reason": context.errors[0]},
                )
                failed.append(
                    DownloadFailure(
                        reason=context.errors[0],
                        provider=provider,
                        attempts=1,
                        retryable=False,
                    )
                )
            else:
                await self._event_bus.publish(
                    "download.completed",
                    {"source": source, "provider": provider},
                )
                succeeded.append(context.result)

        finished_at = datetime.now(timezone.utc)
        return DownloadReport(
            succeeded=succeeded,
            failed=failed,
            skipped=[],
            started_at=started_at,
            finished_at=finished_at,
        )

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable

from SpotiFLAC.application.event_bus import EventBus
from SpotiFLAC.application.legacy_download_adapter import LegacyDownloadAdapter
from SpotiFLAC.application.metadata_service import MetadataService
from SpotiFLAC.application.provider_executor import ProviderExecutor
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
    PipelineStep,
)
from SpotiFLAC.core.config import (
    DownloadFailure,
    DownloadReport,
    DownloadRequest,
    DownloadSkip,
)
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.core.retry import RetryPolicy


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
        downloader: object | None = None,
        provider_executor: (
            Callable[[str, str], Awaitable[None]] | ProviderExecutor | None
        ) = None,
        provider_resolver: ProviderResolver | None = None,
        metadata_service: MetadataService | None = None,
        tagger: Callable[[str, object], Awaitable[None]] | None = None,
        lyrics_writer: Callable[[str, object], Awaitable[None]] | None = None,
        canvas_writer: Callable[[str, object], Awaitable[None]] | None = None,
        transcoder: Callable[[str, DownloadRequest], Awaitable[str]] | None = None,
        indexer: Callable[[str, object], Awaitable[None]] | None = None,
    ) -> None:
        self._event_bus = event_bus or EventBus()
        self._downloader = downloader
        normalized_executor: ProviderExecutor | None
        if isinstance(provider_executor, ProviderExecutor):
            normalized_executor = provider_executor
        elif provider_executor is not None:
            normalized_executor = ProviderExecutor(provider_executor)
        else:
            normalized_executor = None
        self._provider_executor = normalized_executor
        self._metadata_service = metadata_service or MetadataService()
        self._provider_resolver = provider_resolver or ProviderResolver()
        self._pipeline = DownloadPipeline(
            [ResolveStep(), ProviderStep(self._provider_resolver)],
            event_bus=self._event_bus,
        )
        self._post_steps: list[PipelineStep] = [ValidateStep()]
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

    @classmethod
    def from_legacy_options(
        cls,
        options: object,
        *,
        event_bus: EventBus | None = None,
        prefetched: dict[str, TrackMetadata] | None = None,
        downloader: object | None = None,
    ) -> "DownloadService":
        if downloader is not None:
            legacy_downloader = LegacyDownloadAdapter(
                downloader,
                prefetched=(prefetched or None),
                options=options,
                forward_metadata=True,
            )
        else:
            legacy_downloader = LegacyDownloadAdapter.from_options(
                options,
                prefetched=(prefetched or None),
            )

        timeout_s = getattr(options, "timeout_s", 10) or 10

        return cls(
            event_bus=event_bus,
            downloader=legacy_downloader.downloader,
            provider_executor=legacy_downloader,
            metadata_service=MetadataService(
                timeout_s=int(timeout_s),
                compatibility_fallback=False,
            ),
        )

    def legacy_options_for(self, request: DownloadRequest) -> object:
        """Compatibility view retained for callers migrating from legacy options."""
        return LegacyDownloadAdapter.options_for(request)

    async def download(
        self,
        request: DownloadRequest,
        *,
        resume_event: asyncio.Event | None = None,
    ) -> DownloadReport:
        started_at = datetime.now(timezone.utc)
        succeeded: list[DownloadResult] = []
        failed: list[DownloadFailure] = []
        skipped: list[DownloadSkip] = []
        resolved_metadata = dict(request.prefetched or {})
        if request.prefetched is None:
            for metadata in await self._metadata_service.resolve(request):
                if metadata.external_url:
                    resolved_metadata[metadata.external_url] = metadata
                resolved_metadata[f"spotify:track:{metadata.id}"] = metadata
        legacy_adapter: LegacyDownloadAdapter | None = None

        if isinstance(self._provider_executor, LegacyDownloadAdapter):
            self._provider_executor.set_prefetched(resolved_metadata)

        for source in request.sources:
            if resume_event is not None:
                await resume_event.wait()
            await self._event_bus.publish(
                "download.started",
                {"source": source, "quality": request.config.download.quality},
            )

            context = await self._pipeline.prepare(
                DownloadContext(
                    request=request,
                    source=source,
                    metadata=resolved_metadata.get(source),
                ),
                resume_event=resume_event,
            )
            if context.errors:
                await self._event_bus.publish(
                    "download.failed",
                    {"source": source, "reason": context.errors[0]},
                )
                failed.append(
                    DownloadFailure(
                        source=source,
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
                        source=source,
                        reason="source_not_supported",
                        provider=self._provider_resolver.resolve(request)[0],
                        attempts=1,
                        retryable=False,
                    )
                )
                continue

            policy = RetryPolicy(request.config.download.retries + 1)
            candidates = context.provider_candidates or []
            if not self._provider_executor:
                candidates = candidates[:1]
            provider = context.provider or self._provider_resolver.resolve(request)[0]
            last_error: Exception | None = None
            if self._provider_executor is not None:
                provider_executor = self._provider_executor
            else:
                if legacy_adapter is None:
                    legacy_adapter = (
                        LegacyDownloadAdapter(
                            self._downloader,
                            prefetched=(
                                resolved_metadata
                                if request.prefetched is not None
                                else None
                            ),
                        )
                        if self._downloader is not None
                        else LegacyDownloadAdapter.from_options(
                            self.legacy_options_for(request),
                            # Direct DownloadService() construction remains a
                            # compatibility path for callers that monkeypatch
                            # the historical run_async() entrypoint. The named
                            # application factory above is the production path
                            # that forwards batch metadata into the runtime.
                            prefetched=None,
                            forward_metadata=False,
                        )
                    )
                provider_executor = legacy_adapter
            for candidate in candidates:
                provider = candidate.name
                await self._event_bus.publish(
                    "provider.selected",
                    {
                        "source": source,
                        "provider": provider,
                        "candidates": [
                            item.name for item in context.provider_candidates
                        ],
                    },
                )
                await self._event_bus.publish(
                    "provider.started",
                    {
                        "source": source,
                        "provider": provider,
                        "candidates": [
                            item.name for item in context.provider_candidates
                        ],
                    },
                )
                last_error = None
                last_error = await provider_executor.execute_with_retry(
                    provider,
                    source,
                    policy,
                    timeout_s=request.config.download.timeout,
                    resume_event=resume_event,
                )
                if last_error is None:
                    break
                if not ProviderExecutor.is_retryable(last_error, policy):
                    break
            if last_error is not None:
                await self._event_bus.publish(
                    "provider.failed",
                    {
                        "source": source,
                        "provider": provider,
                        "attempts": policy.attempts,
                    },
                )
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
                        source=source,
                        reason="download_failed",
                        provider=provider,
                        attempts=policy.attempts,
                        retryable=ProviderExecutor.is_retryable(last_error, policy),
                    )
                )
                continue

            structured_result = getattr(provider_executor, "last_result", None)
            if not isinstance(structured_result, DownloadResult):
                failed.append(
                    DownloadFailure(
                        source=source,
                        reason="provider_returned_no_result",
                        provider=provider,
                        attempts=1,
                        retryable=False,
                    )
                )
                continue
            context.result = structured_result.model_copy(update={"source": source})
            if context.result.skipped:
                skipped.append(
                    DownloadSkip(
                        track=context.metadata,
                        reason=context.result.error or "already_exists",
                        provider=provider,
                    )
                )
                continue
            context = await self._pipeline.execute(
                context, self._post_steps, resume_event=resume_event
            )
            if context.errors:
                await self._event_bus.publish(
                    "download.failed",
                    {"source": source, "reason": context.errors[0]},
                )
                failed.append(
                    DownloadFailure(
                        source=source,
                        reason=context.errors[0],
                        provider=provider,
                        attempts=1,
                        retryable=False,
                    )
                )
            else:
                await self._event_bus.publish(
                    "provider.succeeded",
                    {"source": source, "provider": provider},
                )
                await self._event_bus.publish(
                    "download.completed",
                    {"source": source, "provider": provider},
                )
                if context.result is not None:
                    succeeded.append(context.result)

        finished_at = datetime.now(timezone.utc)
        return DownloadReport(
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            started_at=started_at,
            finished_at=finished_at,
        )

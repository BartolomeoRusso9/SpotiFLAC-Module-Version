from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Protocol

from SpotiFLAC.core.providers import ProviderCandidate, ProviderResolver
from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

from SpotiFLAC.application.event_bus import EventBus
from SpotiFLAC.application.pause import wait_until_resumed


@dataclass
class DownloadContext:
    request: DownloadRequest
    source: str
    provider: str | None = None
    provider_candidate: ProviderCandidate | None = None
    provider_candidates: list[ProviderCandidate] = field(default_factory=list)
    metadata: TrackMetadata | None = None
    source_file: str | None = None
    output_file: str | None = None
    result: DownloadResult | None = None
    errors: list[str] = field(default_factory=list)


class PipelineStep(Protocol):
    async def execute(self, context: DownloadContext) -> DownloadContext: ...


class ResolveStep:
    async def execute(self, context: DownloadContext) -> DownloadContext:
        is_spotify_urn = context.source.startswith("spotify:")
        is_spotify_url = context.source.startswith(
            ("https://open.spotify.com/", "http://open.spotify.com/")
        )
        if not (is_spotify_urn or is_spotify_url):
            context.errors.append("source_not_supported")
        return context


class ProviderStep:
    def __init__(self, resolver: ProviderResolver) -> None:
        self._resolver = resolver

    async def execute(self, context: DownloadContext) -> DownloadContext:
        candidates = self._resolver.resolve_candidates(context.request)
        if candidates:
            context.provider_candidates = candidates
            context.provider_candidate = candidates[0]
            context.provider = candidates[0].name
        else:
            context.errors.append("no_provider_available")
        return context


class ValidateStep:
    async def execute(self, context: DownloadContext) -> DownloadContext:
        if context.result is None or not context.result.success:
            return context
        if not context.result.file_path or not Path(context.result.file_path).exists():
            return context
        if context.result.file_path.lower().endswith(".flac"):
            from SpotiFLAC.core.flac_validation import validate_flac_file

            valid, message = validate_flac_file(context.result.file_path)
            if not valid:
                context.errors.append(f"validation_failed: {message}")
        return context


class TagStep:
    def __init__(
        self,
        tagger: Callable[[str, TrackMetadata], Awaitable[None]],
    ) -> None:
        self._tagger = tagger

    async def execute(self, context: DownloadContext) -> DownloadContext:
        if not context.result or not context.result.success:
            return context
        if not context.metadata or not context.result.file_path:
            return context
        try:
            await self._tagger(context.result.file_path, context.metadata)
        except Exception as exc:
            context.errors.append(f"tagging_failed: {exc}")
        return context


class LyricsStep:
    def __init__(
        self,
        writer: Callable[[str, TrackMetadata], Awaitable[None]],
    ) -> None:
        self._writer = writer

    async def execute(self, context: DownloadContext) -> DownloadContext:
        if not context.result or not context.result.success:
            return context
        if not context.metadata or not context.result.file_path:
            return context
        try:
            await self._writer(context.result.file_path, context.metadata)
        except Exception as exc:
            context.errors.append(f"lyrics_failed: {exc}")
        return context


class CanvasStep:
    def __init__(
        self,
        writer: Callable[[str, TrackMetadata], Awaitable[None]],
    ) -> None:
        self._writer = writer

    async def execute(self, context: DownloadContext) -> DownloadContext:
        if not context.result or not context.result.success:
            return context
        if not context.metadata or not context.result.file_path:
            return context
        try:
            await self._writer(context.result.file_path, context.metadata)
        except Exception as exc:
            context.errors.append(f"canvas_failed: {exc}")
        return context


class TranscodeStep:
    def __init__(
        self,
        transcoder: Callable[[str, DownloadRequest], Awaitable[str]],
    ) -> None:
        self._transcoder = transcoder

    async def execute(self, context: DownloadContext) -> DownloadContext:
        if (
            not context.result
            or not context.result.success
            or not context.result.file_path
        ):
            return context
        try:
            context.output_file = await self._transcoder(
                context.result.file_path, context.request
            )
        except Exception as exc:
            context.errors.append(f"transcode_failed: {exc}")
        return context


class LibraryIndexStep:
    def __init__(
        self,
        indexer: Callable[[str, DownloadContext], Awaitable[None]],
    ) -> None:
        self._indexer = indexer

    async def execute(self, context: DownloadContext) -> DownloadContext:
        if (
            not context.result
            or not context.result.success
            or not context.result.file_path
        ):
            return context
        try:
            await self._indexer(
                context.output_file or context.result.file_path, context
            )
        except Exception as exc:
            context.errors.append(f"library_index_failed: {exc}")
        return context


class DownloadPipeline:
    def __init__(
        self,
        steps: list[PipelineStep],
        event_bus: EventBus | None = None,
    ) -> None:
        self._steps = steps
        self._event_bus = event_bus

    async def prepare(
        self,
        context: DownloadContext,
        resume_event: asyncio.Event | None = None,
    ) -> DownloadContext:
        return await self.execute(context, self._steps, resume_event=resume_event)

    async def execute(
        self,
        context: DownloadContext,
        steps: list[PipelineStep],
        resume_event: asyncio.Event | None = None,
    ) -> DownloadContext:
        base_payload = {
            "source": context.source,
            "provider": context.provider,
        }
        if self._event_bus:
            await self._event_bus.publish("pipeline.started", base_payload)
        for step in steps:
            await wait_until_resumed(resume_event)
            step_name = type(step).__name__.removesuffix("Step").lower()
            payload = {
                "source": context.source,
                "step": step_name,
                "provider": context.provider,
            }
            if self._event_bus:
                await self._event_bus.publish(
                    "pipeline.phase_changed",
                    {**payload, "phase": step_name},
                )
                await self._event_bus.publish(f"pipeline.{step_name}.started", payload)
            context = await step.execute(context)
            if self._event_bus:
                await self._event_bus.publish(
                    (
                        f"pipeline.{step_name}.failed"
                        if context.errors
                        else f"pipeline.{step_name}.completed"
                    ),
                    {**payload, "errors": list(context.errors)},
                )
            if context.errors:
                break
        if self._event_bus:
            await self._event_bus.publish(
                "pipeline.failed" if context.errors else "pipeline.completed",
                {**base_payload, "errors": list(context.errors)},
            )
        return context

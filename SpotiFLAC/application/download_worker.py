from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from SpotiFLAC.application.post_processing import PostProcessingService
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

TrackExecutor = Callable[[TrackMetadata, str, int], Awaitable[DownloadResult]]
OutputDirectory = Callable[[TrackMetadata], Awaitable[str]]
ResultCallback = Callable[[TrackMetadata, DownloadResult], Awaitable[None]]
StartCallback = Callable[[TrackMetadata, int], Awaitable[None]]


@dataclass
class WorkerReport:
    failed: list[tuple[str, str, str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    completed: dict[str, str] = field(default_factory=dict)
    results: dict[str, DownloadResult] = field(default_factory=dict)


class ApplicationDownloadWorker:
    """Concurrent application worker independent of legacy provider details."""

    def __init__(
        self,
        tracks: list[TrackMetadata],
        *,
        max_concurrent: int,
        executor: TrackExecutor,
        output_directory: OutputDirectory,
        post_processing: PostProcessingService,
        existing_paths: dict[str, Path] | None = None,
        skipped_provider: str = "none",
        positions: list[int] | None = None,
        on_result: ResultCallback | None = None,
        on_start: StartCallback | None = None,
    ) -> None:
        self._tracks = tracks
        self._max_concurrent = max(1, max_concurrent)
        self._executor = executor
        self._output_directory = output_directory
        self._post_processing = post_processing
        self._existing_paths = existing_paths or {}
        self._skipped_provider = skipped_provider
        self._positions = positions or list(range(1, len(tracks) + 1))
        self._on_result = on_result
        self._on_start = on_start

    async def run(self, options: Any) -> WorkerReport:
        report = WorkerReport()
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def process(index: int, track: TrackMetadata) -> None:
            async with semaphore:
                if self._on_start is not None:
                    await self._on_start(track, index)
                existing = self._existing_paths.get(track.id)
                if existing is not None:
                    result = DownloadResult.skipped_result(
                        self._skipped_provider,
                        str(existing),
                    )
                else:
                    output_dir = await self._output_directory(track)
                    try:
                        result = await self._executor(
                            track, output_dir, self._positions[index]
                        )
                    except Exception as exc:
                        result = DownloadResult.fail("none", f"Unexpected error: {exc}")

                result = await self._post_processing.process(result, track, options)
                report.results[track.id] = result.model_copy(
                    update={"source": track.external_url or f"spotify:track:{track.id}"}
                )
                if result.success and result.file_path:
                    report.completed[track.id] = result.file_path
                if result.success and result.skipped:
                    report.skipped.append((track.id, track.title))
                elif not result.success:
                    report.failed.append(
                        (
                            track.id,
                            track.title,
                            track.artists,
                            result.error or "unknown",
                        )
                    )
                if self._on_result is not None:
                    await self._on_result(track, result)

        await asyncio.gather(
            *(process(index, track) for index, track in enumerate(self._tracks))
        )
        return report

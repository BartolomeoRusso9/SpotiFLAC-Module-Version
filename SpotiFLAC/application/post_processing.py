from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.core.models import sanitize
from SpotiFLAC.core.transcode import (
    already_in_target_format,
    result_format_for,
    transcode_file_async,
)

logger = logging.getLogger(__name__)

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


async def apply_post_processing(
    result: DownloadResult,
    metadata: TrackMetadata,
    options: Any,
) -> DownloadResult:
    """Apply output transforms after provider execution.

    This is deliberately application-owned: providers only produce an audio
    result, while transcoding and sidecar writes are output policy.
    """
    if options.transcode_to and not result.skipped:
        result = await _transcode_result(result, options)
        if not result.success:
            return result
    if result.success and not result.skipped:
        await _write_lrc_sidecars(result, metadata, options)
    if result.success:
        await _write_canvas_sidecars(result, metadata, options)
    return result


async def _transcode_result(result: DownloadResult, options: Any) -> DownloadResult:
    if not result.file_path:
        return result
    target_format = options.transcode_to
    if not isinstance(target_format, str):
        return result
    source = Path(result.file_path)
    if await asyncio.to_thread(already_in_target_format, source, target_format):
        return result
    try:
        destination = await transcode_file_async(
            source,
            fmt=target_format,
            bitrate=options.transcode_bitrate,
            keep_original=options.transcode_keep_original,
        )
    except Exception as exc:
        logger.warning("[transcode] %s: %s", source.name, exc)
        return DownloadResult.fail(
            result.provider,
            f"Downloaded, but transcode to {target_format.upper()} failed: {exc}",
        )
    return DownloadResult.ok(
        result.provider,
        str(destination),
        cast(Any, result_format_for(target_format) or "flac"),
    )


async def _write_lrc_sidecars(
    result: DownloadResult,
    metadata: TrackMetadata,
    options: Any,
) -> None:
    if not (options.save_lrc or options.lrc_library_dir) or not result.file_path:
        return

    def write() -> list[str]:
        from SpotiFLAC.core.tagger import read_embedded_tags

        file_path = result.file_path
        if not file_path:
            return []
        audio = Path(file_path)
        lyrics = (read_embedded_tags(audio, include_cover=False).lyrics or "").strip()
        if not lyrics:
            return []
        written: list[str] = []
        if options.save_lrc:
            beside = audio.with_suffix(".lrc")
            beside.write_text(lyrics + "\n", encoding="utf-8")
            written.append(str(beside))
        if options.lrc_library_dir:
            artist = (
                metadata.first_artist if options.first_artist_only else metadata.artists
            )
            library = Path(options.lrc_library_dir).expanduser()
            library.mkdir(parents=True, exist_ok=True)
            collected = library / f"{sanitize(artist)} - {sanitize(metadata.title)}.lrc"
            collected.write_text(lyrics + "\n", encoding="utf-8")
            written.append(str(collected))
        return written

    try:
        for path in await asyncio.to_thread(write):
            logger.info("[lrc] wrote %s", path)
    except Exception as exc:
        logger.warning("[lrc] could not write sidecar for %s: %s", metadata.title, exc)


async def _write_canvas_sidecars(
    result: DownloadResult,
    metadata: TrackMetadata,
    options: Any,
) -> None:
    if not (options.save_canvas or options.canvas_library_dir) or not result.file_path:
        return
    from SpotiFLAC.core.canvas import (
        CANVAS_SUFFIXES,
        download_canvas_async,
        fetch_canvas_async,
    )

    audio = Path(result.file_path)
    artist = metadata.first_artist if options.first_artist_only else metadata.artists
    stem = f"{sanitize(artist)} - {sanitize(metadata.title)}"

    def destinations(suffix: str) -> list[Path]:
        output: list[Path] = []
        if options.save_canvas:
            output.append(audio.with_suffix(suffix))
        if options.canvas_library_dir:
            output.append(
                Path(options.canvas_library_dir).expanduser() / f"{stem}{suffix}"
            )
        return [path for path in output if path != audio]

    try:
        if any(
            all(path.exists() for path in destinations(suffix))
            for suffix in CANVAS_SUFFIXES
        ):
            return
        canvas = await fetch_canvas_async(
            metadata.id, providers=options.canvas_providers or None
        )
        if not canvas:
            return
        targets = [path for path in destinations(canvas.suffix) if not path.exists()]
        if not targets:
            return
        payload = await download_canvas_async(canvas)
        if not payload:
            return

        def write() -> list[str]:
            written: list[str] = []
            for path in targets:
                path.parent.mkdir(parents=True, exist_ok=True)
                partial = path.with_name(path.name + ".part")
                partial.write_bytes(payload)
                partial.replace(path)
                written.append(str(path))
            return written

        for path in await asyncio.to_thread(write):
            logger.info("[canvas] wrote %s (via %s)", path, canvas.provider)
    except Exception as exc:
        logger.warning("[canvas] could not save one for %s: %s", metadata.title, exc)

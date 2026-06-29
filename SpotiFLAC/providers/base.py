from __future__ import annotations

import asyncio
import asyncio.subprocess as _subproc
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Awaitable, Callable

from ..core.http import AsyncHttpClient, AsyncRateLimiter, RetryConfig
from ..core.models import DownloadResult, TrackMetadata, build_filename

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """
    Contratto che ogni provider DEVE rispettare.
    I metodi concreti (stream_download, build_path) evitano
    la duplicazione presente nei file originali.
    """

    name: str = "base"
    _is_async: bool = True

    def __init__(
        self,
        timeout_s: int = 30,
        retry: RetryConfig | None = None,
        headers: dict[str, str] | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self._async_http = AsyncHttpClient(
            provider=self.name,
            timeout_s=timeout_s,
            rate_limiter=rate_limiter,
            headers=headers,
        )
        # Type hint aggiornato per supportare sia callback sincroni (None) che asincroni (Awaitable)
        self._progress_cb: Callable[[int, int], Awaitable[None] | None] | None = None

    def set_progress_callback(
        self, cb: Callable[[int, int], Awaitable[None] | None]
    ) -> None:
        """Imposta il callback di progresso in modo sicuro (Thread-Safe e Async-Safe)."""
        if cb is None:
            self._progress_cb = None
            return

        # Catturiamo il riferimento all'event loop principale
        # nel momento in cui il provider viene inizializzato.
        try:
            main_loop = asyncio.get_running_loop()
        except RuntimeError:
            main_loop = None

        def safe_wrapper(written: int, total: int) -> None:
            res = cb(written, total)
            if asyncio.iscoroutine(res):
                try:
                    # Check whether we are in the main async thread
                    asyncio.get_running_loop()
                    asyncio.create_task(res)
                except RuntimeError:
                    # RuntimeError significa che siamo in un worker thread (es. yt-dlp).
                    # "Teletrasportiamo" l'esecuzione nel loop principale in modo sicuro.
                    if main_loop and main_loop.is_running():
                        asyncio.run_coroutine_threadsafe(res, main_loop)

        self._progress_cb = safe_wrapper

    def set_stop_event(self, ev) -> None:
        """Attach a threading.Event used to signal cancellation to the provider and its HttpClient."""
        try:
            self._stop_event = ev
            if hasattr(self, "_async_http") and self._async_http is not None:
                setattr(self._async_http, "_stop_event", ev)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Interface methods — subclasses must implement
    # ------------------------------------------------------------------

    @abstractmethod
    async def download_track_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format: str = "{title} - {artist}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        allow_fallback: bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
        **kwargs,
    ) -> DownloadResult:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_output_path(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        filename_format: str,
        position: int,
        include_track_num: bool,
        use_album_track_num: bool,
        first_artist_only: bool,
        extension: str = ".flac",
    ) -> Path:
        filename = build_filename(
            metadata,
            fmt=filename_format,
            position=position,
            include_track_number=include_track_num,
            use_album_track_number=use_album_track_num,
            first_artist_only=first_artist_only,
            extension=extension,
        )
        path = Path(output_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _file_exists(self, path: Path) -> bool:
        if path.exists() and path.stat().st_size > 0:
            print(f"Skip (already existing): {path.name}")
            size_mb = path.stat().st_size / (1024 * 1024)
            logger.debug("File already exists: %s (%.2f MB)", path.name, size_mb)
            return True
        return False

    async def _run_ffmpeg(self, *args: str) -> tuple[int, str, str]:
        """Executes ffmpeg asynchronously and returns (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=_subproc.PIPE,
            stderr=_subproc.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode(errors="ignore"),
            stderr.decode(errors="ignore"),
        )

    async def _run_ffprobe(self, *args: str) -> tuple[int, str, str]:
        """Executes ffprobe asynchronously and returns (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=_subproc.PIPE,
            stderr=_subproc.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode(errors="ignore"),
            stderr.decode(errors="ignore"),
        )

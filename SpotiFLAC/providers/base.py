"""
BaseProvider: classe astratta per tutti i provider audio.
Implementa il pattern Protocol/Interface di Go.
"""
from __future__ import annotations
import asyncio
import asyncio.subprocess as _subproc
import inspect
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Awaitable, Callable

from ..core.models import TrackMetadata, DownloadResult, build_filename
from ..core.http import AsyncHttpClient, AsyncRateLimiter, HttpClient, RetryConfig
from ..core.errors import SpotiflacError

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """
    Contratto che ogni provider DEVE rispettare.
    I metodi concreti (stream_download, build_path) evitano
    la duplicazione presente nei file originali.
    """
    name: str = "base"
    _is_async: bool = False

    def __init__(
            self,
            timeout_s:  int            = 30,
            retry:      RetryConfig | None = None,
            headers:    dict[str, str] | None = None,
            rate_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self._http = HttpClient(
            provider  = self.name,
            timeout_s = timeout_s,
            retry     = retry,
            headers   = headers,
        )
        self._async_http = AsyncHttpClient(
            provider    = self.name,
            timeout_s   = timeout_s,
            rate_limiter= rate_limiter,
            headers     = headers,
        )
        self._progress_cb: Callable[[int, int], None] | None = None

    def set_progress_callback(self, cb: Callable[[int, int], None]) -> None:
        self._progress_cb = cb

    def set_stop_event(self, ev) -> None:
        """Attach a threading.Event used to signal cancellation to the provider and its HttpClient."""
        try:
            self._stop_event = ev
            # also propagate to the underlying HttpClient when present
            if hasattr(self, "_http") and self._http is not None:
                setattr(self._http, "_stop_event", ev)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Interface methods — subclasses must implement
    # ------------------------------------------------------------------

    def download_track(
            self,
            metadata:   TrackMetadata,
            output_dir: str,
            *,
            filename_format:      str  = "{title} - {artist}",
            position:             int  = 1,
            include_track_num:    bool = False,
            use_album_track_num:  bool = False,
            first_artist_only:    bool = False,
            allow_fallback:       bool = True,
            embed_lyrics:         bool = False,
            lyrics_providers:     list[str] | None = None,
            enrich_metadata:      bool = False,
            enrich_providers:     list[str] | None = None,
            is_album:             bool = False,
            **kwargs,
    ) -> DownloadResult | Awaitable[DownloadResult]:
        own_async_is_overridden = (
            type(self).download_track_async is not BaseProvider.download_track_async
        )

        if not own_async_is_overridden:
            raise NotImplementedError(
                f"{type(self).__name__} non implementa né download_track (sync) "
                f"né download_track_async: deve sovrascrivere almeno uno dei due."
            )

        coro = self.download_track_async(
            metadata,
            output_dir,
            filename_format=filename_format,
            position=position,
            include_track_num=include_track_num,
            use_album_track_num=use_album_track_num,
            first_artist_only=first_artist_only,
            allow_fallback=allow_fallback,
            embed_lyrics=embed_lyrics,
            lyrics_providers=lyrics_providers,
            enrich_metadata=enrich_metadata,
            enrich_providers=enrich_providers,
            is_album=is_album,
            **kwargs,
        )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Nessun event loop attivo nel thread corrente: possiamo usare asyncio.run.
            return asyncio.run(coro)
        else:
            # Già dentro un event loop: non possiamo bloccare con asyncio.run.
            # Eseguiamo la coroutine in un nuovo loop su un thread separato.
            import concurrent.futures

            def _run_in_new_loop():
                return asyncio.run(coro)

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_run_in_new_loop).result()

    async def download_track_async(
            self,
            metadata:   TrackMetadata,
            output_dir: str,
            *,
            filename_format:      str  = "{title} - {artist}",
            position:             int  = 1,
            include_track_num:    bool = False,
            use_album_track_num:  bool = False,
            first_artist_only:    bool = False,
            allow_fallback:       bool = True,
            embed_lyrics:         bool = False,
            lyrics_providers:     list[str] | None = None,
            enrich_metadata:      bool = False,
            enrich_providers:     list[str] | None = None,
            is_album:             bool = False,
            **kwargs,
    ) -> DownloadResult:
        own_sync_is_overridden = (
            type(self).download_track is not BaseProvider.download_track
        )

        if own_sync_is_overridden:
            return await asyncio.to_thread(
                self.download_track,
                metadata,
                output_dir,
                filename_format=filename_format,
                position=position,
                include_track_num=include_track_num,
                use_album_track_num=use_album_track_num,
                first_artist_only=first_artist_only,
                allow_fallback=allow_fallback,
                embed_lyrics=embed_lyrics,
                lyrics_providers=lyrics_providers,
                enrich_metadata=enrich_metadata,
                enrich_providers=enrich_providers,
                is_album=is_album,
                **kwargs,
            )

        # Né download_track né download_track_async sono stati sovrascritti
        # dalla sottoclasse: configurazione non valida del provider.
        raise NotImplementedError(
            f"{type(self).__name__} non implementa download_track_async "
            f"né download_track: deve sovrascrivere almeno uno dei due."
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_output_path(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            filename_format:     str,
            position:            int,
            include_track_num:   bool,
            use_album_track_num: bool,
            first_artist_only:   bool,
            extension:           str = ".flac",
    ) -> Path:
        filename = build_filename(
            metadata,
            fmt                  = filename_format,
            position             = position,
            include_track_number    = include_track_num,
            use_album_track_number  = use_album_track_num,
            first_artist_only    = first_artist_only,
            extension            = extension,
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
        return proc.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")

    async def _run_ffprobe(self, *args: str) -> tuple[int, str, str]:
        """Executes ffprobe asynchronously and returns (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=_subproc.PIPE,
            stderr=_subproc.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")
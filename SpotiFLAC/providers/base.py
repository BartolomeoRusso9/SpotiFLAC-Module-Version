"""
BaseProvider: classe astratta per tutti i provider audio.
Implementa il pattern Protocol/Interface di Go.
"""
from __future__ import annotations
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from ..core.models import TrackMetadata, DownloadResult, build_filename
from ..core.http import HttpClient, RetryConfig
from ..core.errors import SpotiflacError

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """
    Contratto che ogni provider DEVE rispettare.
    I metodi concreti (stream_download, build_path) evitano
    la duplicazione presente nei file originali.
    """
    name: str = "base"

    def __init__(
        self,
        timeout_s:  int            = 30,
        retry:      RetryConfig | None = None,
        headers:    dict[str, str] | None = None,
    ) -> None:
        self._http = HttpClient(
            provider  = self.name,
            timeout_s = timeout_s,
            retry     = retry,
            headers   = headers,
        )
        self._progress_cb: Callable[[int, int], None] | None = None

    def set_progress_callback(self, cb: Callable[[int, int], None]) -> None:
        self._progress_cb = cb

    # ------------------------------------------------------------------
    # Interface methods — subclasses must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def download_track(
        self,
        metadata:   TrackMetadata,
        output_dir: str,
        *,
        filename_format:     str  = "{title} - {artist}",
        position:            int  = 1,
        include_track_num:   bool = False,
        use_album_track_num: bool = False,
        first_artist_only:   bool = False,
        allow_fallback:      bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        lyrics_spotify_token: str = "",
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
    ) -> DownloadResult:
        """Scarica la traccia e ritorna un DownloadResult (mai eccezioni al caller)."""
        raise NotImplementedError

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

    def _safe_download(
        self,
        metadata:   TrackMetadata,
        output_dir: str,
        **kwargs,
    ) -> DownloadResult:
        """
        Wrapper che cattura SpotiflacError e ritorna DownloadResult.fail()
        invece di propagare eccezioni — equivalente al pattern Go
        `if err != nil { return err }`.
        """
        try:
            return self.download_track(metadata, output_dir, **kwargs)
        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] Unexpected error", self.name)
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

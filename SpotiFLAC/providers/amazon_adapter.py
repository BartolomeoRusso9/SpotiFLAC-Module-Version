"""
Adapter che avvolge il legacy AmazonDownloader nell'interfaccia BaseProvider.
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

from ..core.models import TrackMetadata, DownloadResult, build_filename
from ..core.errors import SpotiflacError
from .base import BaseProvider

logger = logging.getLogger(__name__)


class AmazonProvider(BaseProvider):
    name = "amazon"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        from .amazon import AmazonDownloader
        self._dl = AmazonDownloader(timeout=timeout_s)

    def set_progress_callback(self, cb) -> None:
        super().set_progress_callback(cb)
        if hasattr(self._dl, "set_progress_callback"):
            self._dl.set_progress_callback(cb)

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
        **kwargs,
    ) -> DownloadResult:
        try:
            downloaded_file = self._dl.download_by_spotify_id(
                spotify_track_id      = metadata.id,
                output_dir            = output_dir,
                isrc                  = metadata.isrc,
                filename_format       = "temp_amazon",
                include_track_number  = include_track_num,
                position              = metadata.track_number or position,
                spotify_track_name    = metadata.title,
                spotify_artist_name   = metadata.artists,
                spotify_album_name    = metadata.album,
                spotify_album_artist  = metadata.album_artist,
                spotify_release_date  = metadata.release_date,
                use_album_track_number = use_album_track_num,
                spotify_cover_url     = metadata.cover_url,
            )
            if downloaded_file and os.path.exists(downloaded_file):
                # rinomina al formato atteso
                dest = self._build_output_path(
                    metadata, output_dir, filename_format,
                    position, include_track_num, use_album_track_num,
                    first_artist_only, extension=".m4a",
                )
                if os.path.abspath(downloaded_file) != os.path.abspath(str(dest)):
                    import shutil
                    shutil.move(downloaded_file, str(dest))
                return DownloadResult.ok(self.name, str(dest), fmt="m4a")
            return DownloadResult.fail(self.name, "No file produced")
        except SpotiflacError as exc:
            logger.error("[amazon] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[amazon] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

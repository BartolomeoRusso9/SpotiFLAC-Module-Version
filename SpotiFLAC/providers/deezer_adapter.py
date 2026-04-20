"""
Adapter che avvolge il legacy DeezerDownloader nell'interfaccia BaseProvider.
Deezer usa asyncio internamente — lo eseguiamo con asyncio.run().
"""
from __future__ import annotations
import asyncio
import glob
import logging
import os

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError
from .base import BaseProvider

logger = logging.getLogger(__name__)


class DeezerProvider(BaseProvider):
    name = "deezer"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        from .deezer import DeezerDownloader
        self._dl = DeezerDownloader()

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
        if not metadata.isrc:
            return DownloadResult.fail(self.name, "No ISRC available for Deezer")

        try:
            before = set(glob.glob(os.path.join(output_dir, "*.flac")))
            ok = asyncio.run(self._dl.download_by_isrc(metadata.isrc, output_dir))
            if not ok:
                return DownloadResult.fail(self.name, "Deezer download_by_isrc returned False")

            after     = set(glob.glob(os.path.join(output_dir, "*.flac")))
            new_files = after - before
            if not new_files:
                return DownloadResult.fail(self.name, "No new FLAC file found after download")

            downloaded = max(new_files, key=os.path.getctime)

            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if os.path.abspath(downloaded) != os.path.abspath(str(dest)):
                import shutil
                shutil.move(downloaded, str(dest))

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[deezer] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[deezer] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

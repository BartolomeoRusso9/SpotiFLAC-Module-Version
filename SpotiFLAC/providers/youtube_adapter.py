"""
Adapter che avvolge il legacy YouTubeDownloader nell'interfaccia BaseProvider.
"""
from __future__ import annotations
import logging
import os

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError
from .base import BaseProvider

logger = logging.getLogger(__name__)


class YouTubeProvider(BaseProvider):
    name = "youtube"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        from .youtube import YouTubeDownloader
        self._dl = YouTubeDownloader()

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
                spotify_track_id     = metadata.id,
                output_dir           = output_dir,
                spotify_track_name   = metadata.title,
                spotify_artist_name  = metadata.artists,
                spotify_album_name   = metadata.album,
                spotify_album_artist = metadata.album_artist,
                spotify_release_date = metadata.release_date,
                spotify_track_number = metadata.track_number or position,
                spotify_total_tracks = metadata.total_tracks,
                spotify_disc_number  = metadata.disc_number,
                spotify_total_discs  = metadata.total_discs,
                spotify_cover_url    = metadata.cover_url,
            )
            if downloaded_file and os.path.exists(downloaded_file):
                dest = self._build_output_path(
                    metadata, output_dir, filename_format,
                    position, include_track_num, use_album_track_num,
                    first_artist_only, extension=".mp3",
                )
                if os.path.abspath(downloaded_file) != os.path.abspath(str(dest)):
                    import shutil
                    shutil.move(downloaded_file, str(dest))
                return DownloadResult.ok(self.name, str(dest), fmt="mp3")
            return DownloadResult.fail(self.name, "No file produced")
        except SpotiflacError as exc:
            logger.error("[youtube] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[youtube] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

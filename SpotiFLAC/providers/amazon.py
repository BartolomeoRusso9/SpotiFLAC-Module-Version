# amazon_provider.py
from __future__ import annotations

import logging
import os
import re
import subprocess
import base64
from typing import Callable

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from mutagen.mp4 import MP4, MP4Cover

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _sanitize(value: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", value).strip()

def _first_artist(artist_str: str) -> str:
    if not artist_str:
        return "Unknown"
    return artist_str.split(",")[0].strip()

def _safe_int(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0

def _ffmpeg_path() -> str:
    return "ffmpeg"

def _ffprobe_path() -> str:
    return "ffprobe"


# ---------------------------------------------------------------------------
# AmazonProvider
# ---------------------------------------------------------------------------

class AmazonProvider(BaseProvider):
    name = "amazon"

    def __init__(self, timeout_s: int = 120) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})

    def set_progress_callback(self, cb: Callable[[int, int], None]) -> None:
        super().set_progress_callback(cb)

    # ------------------------------------------------------------------
    # Songlink → Amazon URL
    # ------------------------------------------------------------------

    def _get_amazon_url(self, spotify_track_id: str) -> str:
        url = f"https://song.link/s/{spotify_track_id}"
        try:
            resp = self._session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
                timeout=10,
            )
            resp.raise_for_status()
            match = re.search(
                r'https://music\.amazon\.com/(tracks|albums)/([A-Z0-9]{10})',
                resp.text,
            )
            if not match:
                raise RuntimeError("Amazon link not found in Songlink HTML")
            asin = match.group(2)
            base = base64.b64decode("aHR0cHM6Ly9tdXNpYy5hbWF6b24uY29tL3RyYWNrcy8=").decode()
            amazon_url = f"{base}{asin}?musicTerritory=US"
            logger.info("[amazon] Resolved URL: %s", amazon_url)
            return amazon_url
        except Exception as exc:
            raise RuntimeError(f"Failed to resolve Amazon URL: {exc}") from exc

    # ------------------------------------------------------------------
    # Download + decrypt
    # ------------------------------------------------------------------

    def _get_codec(self, filepath: str) -> str:
        try:
            cmd = [
                _ffprobe_path(), "-v", "quiet", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath,
            ]
            si = None
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.check_output(cmd, text=True, startupinfo=si).strip()
        except Exception:
            return "m4a"

    def _download_from_afkar(self, amazon_url: str, output_dir: str) -> str:
        asin_match = re.search(r"(B[0-9A-Z]{9})", amazon_url)
        if not asin_match:
            raise RuntimeError(f"Cannot extract ASIN from: {amazon_url}")
        asin = asin_match.group(1)

        api_url = f"https://amzn.afkarxyz.qzz.io/api/track/{asin}"
        logger.info("[amazon] Fetching track (ASIN: %s)", asin)

        resp = self._session.get(api_url, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Amazon API returned status {resp.status_code}")

        data           = resp.json()
        stream_url     = data.get("streamUrl")
        decryption_key = data.get("decryptionKey")

        if not stream_url:
            raise RuntimeError("No streamUrl in API response")

        temp_file = os.path.join(output_dir, f"{asin}.enc")
        logger.info("[amazon] Downloading encrypted stream…")

        with self._session.get(stream_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total      = int(r.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(temp_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if self._progress_cb and total:
                            self._progress_cb(downloaded, total)

        if decryption_key:
            logger.info("[amazon] Decrypting…")
            codec = self._get_codec(temp_file)
            ext   = ".flac" if codec == "flac" else ".m4a"
            out   = os.path.join(output_dir, f"{asin}{ext}")

            si = None
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            result = subprocess.run(
                [_ffmpeg_path(), "-y", "-decryption_key", decryption_key.strip(),
                 "-i", temp_file, "-c", "copy", out],
                capture_output=True, startupinfo=si,
            )
            os.remove(temp_file)
            if result.returncode != 0:
                raise RuntimeError(f"Decryption failed: {result.stderr.decode()}")
            return out

        final = os.path.join(output_dir, f"{asin}.m4a")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)
        return final

    # ------------------------------------------------------------------
    # Metadata embedding
    # ------------------------------------------------------------------

    def _embed_metadata(
            self,
            filepath:     str,
            title:        str,
            artist:       str,
            album:        str,
            album_artist: str,
            date:         str,
            track_num:    int,
            total_tracks: int,
            disc_num:     int,
            total_discs:  int,
            cover_url:    str,
            copyright:    str = "",
            publisher:    str = "",
            url:          str = "",
    ) -> None:
        cover_data: bytes | None = None
        if cover_url:
            try:
                r = self._session.get(cover_url, timeout=15)
                if r.status_code == 200:
                    cover_data = r.content
            except Exception as exc:
                logger.warning("[amazon] Cover download failed: %s", exc)

        t_num   = track_num   or 1
        t_total = total_tracks or 1
        d_num   = disc_num    or 1
        d_total = total_discs or 1

        try:
            if filepath.endswith(".flac"):
                audio = FLAC(filepath)
                audio.delete()
                audio["TITLE"]       = title
                audio["ARTIST"]      = artist
                audio["ALBUM"]       = album
                audio["ALBUMARTIST"] = album_artist
                audio["DATE"]        = date
                audio["TRACKNUMBER"] = str(t_num)
                audio["TRACKTOTAL"]  = str(t_total)
                audio["DISCNUMBER"]  = str(d_num)
                audio["DISCTOTAL"]   = str(d_total)
                if copyright: audio["COPYRIGHT"]    = copyright
                if publisher: audio["ORGANIZATION"] = publisher
                if url:       audio["URL"]          = url
                if cover_data:
                    pic      = Picture()
                    pic.data = cover_data
                    pic.type = PictureType.COVER_FRONT
                    pic.mime = "image/jpeg"
                    audio.add_picture(pic)
                audio.save()

            elif filepath.endswith(".m4a"):
                audio = MP4(filepath)
                audio.delete()
                audio["\xa9nam"] = title
                audio["\xa9ART"] = artist
                audio["\xa9alb"] = album
                audio["aART"]    = album_artist
                audio["\xa9day"] = date
                audio["trkn"]    = [(t_num, t_total)]
                audio["disk"]    = [(d_num, d_total)]
                if copyright: audio["cprt"] = copyright
                if cover_data:
                    audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()

            logger.info("[amazon] Metadata embedded: %s", os.path.basename(filepath))
        except Exception as exc:
            logger.warning("[amazon] embed_metadata failed: %s", exc)

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
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
            artist       = _first_artist(metadata.artists) if first_artist_only else metadata.artists
            album_artist = _first_artist(metadata.album_artist) if first_artist_only else metadata.album_artist

            track_num = position
            if use_album_track_num and _safe_int(metadata.track_number) > 0:
                track_num = _safe_int(metadata.track_number)

            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            amazon_url   = self._get_amazon_url(metadata.id)
            downloaded   = self._download_from_afkar(amazon_url, output_dir)

            ext      = os.path.splitext(downloaded)[1] or ".m4a"
            dest_ext = str(dest).rsplit(".", 1)[0] + ext

            if os.path.abspath(downloaded) != os.path.abspath(dest_ext):
                if os.path.exists(dest_ext):
                    os.remove(dest_ext)
                os.replace(downloaded, dest_ext)

            self._embed_metadata(
                filepath     = dest_ext,
                title        = metadata.title,
                artist       = artist,
                album        = metadata.album,
                album_artist = album_artist,
                date         = metadata.release_date,
                track_num    = track_num,
                total_tracks = _safe_int(metadata.total_tracks),
                disc_num     = _safe_int(metadata.disc_number),
                total_discs  = _safe_int(metadata.total_discs),
                cover_url    = metadata.cover_url,
            )

            return DownloadResult.ok(self.name, dest_ext)

        except SpotiflacError as exc:
            logger.error("[amazon] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[amazon] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

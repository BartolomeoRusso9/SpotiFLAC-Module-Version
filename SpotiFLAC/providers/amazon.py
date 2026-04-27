# amazon_provider.py
from __future__ import annotations

import logging
import os
import re
import subprocess
import base64
import hashlib
from typing import Callable

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from mutagen.mp4 import MP4, MP4Cover

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError
from .base import BaseProvider
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from ..core.console import print_source_banner

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

AMAZON_API_BASE = "https://amazon.spotbye.qzz.io/api"
# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

_AMAZON_DEBUG_KEY_SEED = b"spotif" + b"lac:am" + b"azon:spotbye:api:v1"
_AMAZON_DEBUG_KEY_AAD  = bytes([
    0x61,0x6d,0x61,0x7a,0x6f,0x6e,0x7c,0x73,0x70,0x6f,0x74,0x62,
    0x79,0x65,0x7c,0x64,0x65,0x62,0x75,0x67,0x7c,0x76,0x31,
])
_AMAZON_DEBUG_KEY_NONCE = bytes([
    0x52,0x1f,0xa4,0x9c,0x13,0x77,0x5b,0xe2,0x81,0x44,0x90,0x6d,
])
_AMAZON_DEBUG_KEY_CIPHERTEXT_TAG = bytes([
    0x5b,0xf9,0xc1,0x2e,0x58,0xf8,0x5b,0xc0,0x04,0x68,0x7e,0xff,
    0x3d,0xd6,0x8b,0xe3,0x86,0x49,0x6c,0xfd,0xc1,0x49,0x0b,0xfb,
    0x6c,0x21,0x98,0x51,0xf2,0x38,0x4b,0x4a,0x23,0xe1,0xc6,0xd7,
    0x65,0x7f,0xfb,0xa1,
])

_amazon_debug_key: str | None = None

def _get_amazon_debug_key() -> str:
    global _amazon_debug_key
    if _amazon_debug_key is not None:
        return _amazon_debug_key
    key = hashlib.sha256(_AMAZON_DEBUG_KEY_SEED).digest()
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(
        _AMAZON_DEBUG_KEY_NONCE,
        _AMAZON_DEBUG_KEY_CIPHERTEXT_TAG,
        _AMAZON_DEBUG_KEY_AAD,
    )
    _amazon_debug_key = plaintext.decode()
    return _amazon_debug_key


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
                timeout=20,
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

    def _download_from_api(self, amazon_url: str, output_dir: str, quality: str) -> str:
        asin_match = re.search(r"(B[0-9A-Z]{9})", amazon_url)
        if not asin_match:
            raise RuntimeError(f"Cannot extract ASIN from: {amazon_url}")
        asin = asin_match.group(1)

        api_url = f"{AMAZON_API_BASE}/track/{asin}"
        logger.info("[amazon] Fetching track (ASIN: %s)", asin)

        print_source_banner("amazon", api_url, quality)

        debug_key = _get_amazon_debug_key()
        resp = self._session.get(
            api_url,
            headers={"X-Debug-Key": debug_key},
            timeout=30,
        )
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
    # Metadata embedding (fallback for .m4a only)
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
            filename_format:     str             = "{title} - {artist}",
            position:            int             = 1,
            include_track_num:   bool            = False,
            use_album_track_num: bool            = False,
            first_artist_only:   bool            = False,
            allow_fallback:      bool            = True,
            quality:             str             = "LOSSLESS",
            # ── parametri lyrics e enrich (stessa firma di Tidal/Qobuz) ──
            embed_lyrics:            bool            = False,
            lyrics_providers:        list[str] | None = None,
            lyrics_spotify_token:    str             = "",
            enrich_metadata:         bool            = False,
            enrich_providers:        list[str] | None = None,
            **kwargs,
    ) -> DownloadResult:
        try:
            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            # Avvia MusicBrainz in parallelo mentre il file viene scaricato
            from ..core.musicbrainz import AsyncMBFetch
            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None

            amazon_url = self._get_amazon_url(metadata.id)
            downloaded = self._download_from_api(amazon_url, output_dir, quality)

            ext      = os.path.splitext(downloaded)[1] or ".m4a"
            dest_ext = str(dest).rsplit(".", 1)[0] + ext

            if os.path.abspath(downloaded) != os.path.abspath(dest_ext):
                if os.path.exists(dest_ext):
                    os.remove(dest_ext)
                os.replace(downloaded, dest_ext)

            # ── MusicBrainz tags ──────────────────────────────────────────
            mb_tags: dict[str, str] = {}
            if mb_fetcher:
                res = mb_fetcher.result()
                if res:
                    mapping = {
                        "mbid_track":       "MUSICBRAINZ_TRACKID",
                        "mbid_album":       "MUSICBRAINZ_ALBUMID",
                        "mbid_artist":      "MUSICBRAINZ_ARTISTID",
                        "mbid_relgroup":    "MUSICBRAINZ_RELEASEGROUPID",
                        "mbid_albumartist": "MUSICBRAINZ_ALBUMARTISTID",
                        "barcode":          "BARCODE",
                        "label":            "LABEL",
                        "organization":     "ORGANIZATION",
                        "country":          "RELEASECOUNTRY",
                        "script":           "SCRIPT",
                        "status":           "RELEASESTATUS",
                        "media":            "MEDIA",
                        "type":             "RELEASETYPE",
                        "artist_sort":      "ARTISTSORT",
                        "albumartist_sort": "ALBUMARTISTSORT",
                        "catalognumber":    "CATALOGNUMBER",
                        "bpm":              "BPM",
                        "genre":            "GENRE"
                    }
                    for mb_key, tag_name in mapping.items():
                        val = res.get(mb_key)
                        if val:
                            mb_tags[tag_name] = str(val)
                    if res.get("original_date"):
                        mb_tags["ORIGINALDATE"] = res["original_date"]
                        mb_tags["ORIGINALYEAR"] = res["original_date"][:4]
                    if res.get("catalognumber"):
                        mb_tags["CATALOGNUMBER"] = res["catalognumber"]

                from ..core.tagger import _print_mb_summary
                _print_mb_summary(mb_tags)

            # ── Embedding ────────────────────────────────────────────────
            # FLAC → pipeline centrale (enrich, lyrics, MusicBrainz, copertina HD)
            # M4A  → embedding base (mutagen MP4 non supporta enrich/lyrics FLAC)
            if dest_ext.endswith(".flac"):
                from ..core.tagger import embed_metadata as _embed
                _embed(
                    dest_ext, metadata,
                    first_artist_only       = first_artist_only,
                    cover_url               = metadata.cover_url,
                    session                 = self._session,
                    extra_tags              = mb_tags,
                    embed_lyrics            = embed_lyrics,
                    lyrics_providers        = lyrics_providers,
                    lyrics_spotify_token    = lyrics_spotify_token,
                    enrich                  = enrich_metadata,
                    enrich_providers        = enrich_providers,
                )
            else:
                # Fallback .m4a: tag base senza enrich/lyrics
                track_num    = position
                if use_album_track_num and _safe_int(metadata.track_number) > 0:
                    track_num = _safe_int(metadata.track_number)
                artist       = _first_artist(metadata.artists) if first_artist_only else metadata.artists
                album_artist = _first_artist(metadata.album_artist) if first_artist_only else metadata.album_artist

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
                if enrich_metadata or embed_lyrics:
                    logger.warning(
                        "[amazon] enrich/lyrics non supportati per file .m4a — "
                        "il file deve essere FLAC per abilitarli"
                    )

            fmt = "flac" if dest_ext.endswith(".flac") else "m4a"
            return DownloadResult.ok(self.name, dest_ext, fmt=fmt)

        except SpotiflacError as exc:
            logger.error("[amazon] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[amazon] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
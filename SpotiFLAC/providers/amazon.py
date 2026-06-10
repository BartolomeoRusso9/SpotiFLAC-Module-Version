# amazon_provider.py
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import json
import httpx
import threading
import subprocess
import time
from typing import Callable
from ..core.http import NetworkManager
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from mutagen.mp4 import MP4, MP4Cover

from .base import BaseProvider
from ..core.console import print_source_banner
from ..core.errors import SpotiflacError, ErrorKind
from ..core.models import TrackMetadata, DownloadResult
from ..core.musicbrainz import mb_result_to_tags
from ..core.tagger import embed_metadata, EmbedOptions

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_SQUID_BASE = "https://amz.squid.wtf/api"
_SQUID_UA   = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

API_ENDPOINTS = {
    "spotbye1": {
        "base_url": "https://amz.spotbye.qzz.io/api",
        "method": "POST"
    },
    "spotbye2": {
        "base_url": "https://amazon.spotbye.qzz.io/api",
        "method": "GET"
    },
    "zarz": {
        "base_url": "https://api.zarz.moe/v1/dl/amazeamazeamaze",
        "method": "GET"
    }
}

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
    _amazon_debug_key = plaintext.decode().strip()
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

def _fix_image_url(url: str, size: int = 1000) -> str:
    """Forces Amazon image URLs to high-resolution based on JS implementation."""
    if not url:
        return ""
    cleaned = re.sub(r'\._[^.]+_\.', '.', url)
    if 'images/I/' in cleaned or 'images/S/' in cleaned:
        base, ext = os.path.splitext(cleaned)
        return f"{base}._SL{size}_{ext}"
    return cleaned

def _ffmpeg_path() -> str:
    return "ffmpeg"

def _ffprobe_path() -> str:
    return "ffprobe"

# ---------------------------------------------------------------------------
# AmazonProvider
# ---------------------------------------------------------------------------

class AmazonProvider(BaseProvider):
    name = "amazon"
    _prefetch_thread: threading.Thread | None = None

    def __init__(self, timeout_s: int = 120) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = NetworkManager.get_sync_client()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})
        self._squid_token: str | None = None

    def set_progress_callback(self, cb: Callable[[int, int], None]) -> None:
        super().set_progress_callback(cb)

    def _make_api_request(
            self,
            provider_key: str,
            endpoint: str,
            headers: dict | None = None,
            params:  dict | None = None,
            payload: dict | None = None
    ) -> httpx.Response:
        config = API_ENDPOINTS.get(provider_key)
        if not config:
            raise ValueError(f"Unknown provider: {provider_key}")

        url = f"{config['base_url']}{endpoint}"
        method = config.get("method", "GET").upper()

        if method == "POST":
            return self._session.post(url, json=payload, headers=headers, timeout=30)
        return self._session.get(url, params=params, headers=headers, timeout=30)

    # ------------------------------------------------------------------
    # Songlink / Fallback -> Amazon URL Resolver
    # ------------------------------------------------------------------

    def _format_amazon_url(self, raw_url: str) -> str:
        asin_match = re.search(r'([A-Z0-9]{10})', raw_url.upper())
        if not asin_match:
            raise RuntimeError(f"Failed to extract ASIN from resolved URL: {raw_url}")
        asin = asin_match.group(1)
        base = base64.b64decode("aHR0cHM6Ly9tdXNpYy5hbWF6b24uY29tL3RyYWNrcy8=").decode()
        return f"{base}{asin}?musicTerritory=US"

    def _extract_amazon_from_json_ld(self, obj) -> str | None:
        if isinstance(obj, list):
            for item in obj:
                res = self._extract_amazon_from_json_ld(item)
                if res: return res
        elif isinstance(obj, dict):
            same_as = obj.get("sameAs", [])
            if isinstance(same_as, list):
                for link in same_as:
                    if isinstance(link, str) and "music.amazon." in link:
                        return link
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    res = self._extract_amazon_from_json_ld(v)
                    if res: return res
        return None

    def _resolve_via_songstats(self, isrc: str) -> str | None:
        url = f"https://songstats.com/{isrc.upper().strip()}?ref=ISRCFinder"
        try:
            resp = self._session.get(url, headers={"User-Agent": _DEFAULT_UA, "Accept": "text/html"}, timeout=15)
            if resp.status_code == 200:
                for match in re.finditer(r'<script type="application/ld\+json">([\s\S]*?)</script>', resp.text):
                    try:
                        data = json.loads(match.group(1))
                        amz_url = self._extract_amazon_from_json_ld(data)
                        if amz_url:
                            logger.info("[amazon] Resolved via Songstats ISRC")
                            return amz_url
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"[amazon] Songstats failed: {e}")
        return None

    def _resolve_amazon_url(self, metadata: TrackMetadata) -> str:
        """Multilayer resolver ported from JS checkAvailability/resolveAmazonURL"""
        if metadata.id and re.match(r'^B[0-9A-Z]{9}$', metadata.id.upper()):
            logger.info(f"[amazon] ID {metadata.id} is already an ASIN.")
            return self._format_amazon_url(f"https://music.amazon.com/tracks/{metadata.id}")

        track_id = metadata.id

        # 1. ZARZ.MOE API (Spotify ID)
        source_url = f"https://open.spotify.com/track/{track_id}"
        try:
            resp = self._session.post(
                "https://api.zarz.moe/v1/resolve",
                json={"url": source_url},
                headers={"User-Agent": "SpotiFLAC-Mobile/4.5.0"},
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and "AmazonMusic" in data.get("songUrls", {}):
                    amz_val = data["songUrls"]["AmazonMusic"]
                    amazon_url = amz_val[0] if isinstance(amz_val, list) and amz_val else amz_val
                    if amazon_url:
                        logger.info("[amazon] Resolved via Zarz.moe API")
                        return self._format_amazon_url(amazon_url)
        except Exception as exc:
            logger.warning(f"[amazon] Zarz.moe resolve failed: {exc}")

        # 2. SONGLINK API (Deezer ID)
        deezer_id = getattr(metadata, "deezer_id", None)
        if deezer_id:
            try:
                dz_url = f"https://www.deezer.com/track/{deezer_id}"
                sl_api_url = f"https://api.song.link/v1-alpha.1/links?url={dz_url}&userCountry=US"
                resp = self._session.get(sl_api_url, headers={"User-Agent": _DEFAULT_UA}, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    links = data.get("linksByPlatform", {})
                    if "amazonMusic" in links:
                        logger.info("[amazon] Resolved via SongLink API (Deezer ID)")
                        return self._format_amazon_url(links["amazonMusic"].get("url"))
            except Exception as exc:
                logger.warning(f"[amazon] SongLink API (Deezer ID) failed: {exc}")

        # 3. SONGLINK HTML (Spotify ID Fallback)
        try:
            sl_url = f"https://song.link/s/{track_id}"
            resp = self._session.get(sl_url, headers={"User-Agent": _DEFAULT_UA}, timeout=15)
            if resp.status_code == 200:
                asin_match = re.search(r'trackAsin=([A-Z0-9]{10})', resp.text)
                if not asin_match:
                    asin_match = re.search(r'https://music\.amazon\.com/tracks/([A-Z0-9]{10})', resp.text)
                if asin_match:
                    logger.info("[amazon] Resolved via Songlink HTML Scraping")
                    return self._format_amazon_url(asin_match.group(1))
        except Exception as exc:
            logger.warning(f"[amazon] Songlink HTML failed: {exc}")

        # 4. SONGLINK API (Spotify ID)
        try:
            sl_api_url = f"https://api.song.link/v1-alpha.1/links?url={source_url}&userCountry=US"
            resp = self._session.get(sl_api_url, headers={"User-Agent": _DEFAULT_UA}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                links = data.get("linksByPlatform", {})
                if "amazonMusic" in links:
                    logger.info("[amazon] Resolved via SongLink API (Spotify ID)")
                    return self._format_amazon_url(links["amazonMusic"].get("url"))
        except Exception as exc:
            logger.warning(f"[amazon] SongLink API resolve failed: {exc}")

        # 5. ISRC FALLBACKS (SongLink API -> SongStats)
        if getattr(metadata, "isrc", None):
            isrc = metadata.isrc
            try:
                sl_api_url = f"https://api.song.link/v1-alpha.1/links?isrc={isrc}&userCountry=US"
                resp = self._session.get(sl_api_url, headers={"User-Agent": _DEFAULT_UA}, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    links = data.get("linksByPlatform", {})
                    if "amazonMusic" in links:
                        logger.info("[amazon] Resolved via SongLink API (ISRC)")
                        return self._format_amazon_url(links["amazonMusic"].get("url"))
            except Exception as exc:
                logger.warning(f"[amazon] SongLink API (ISRC) failed: {exc}")

            amz_url = self._resolve_via_songstats(isrc)
            if amz_url:
                return self._format_amazon_url(amz_url)

        raise RuntimeError(f"Could not resolve Amazon URL for {track_id} via any method.")

    # ------------------------------------------------------------------
    # Squid PoW Captcha + Direct FLAC Download
    # ------------------------------------------------------------------

    def _solve_pow(self, challenge: dict) -> dict:
        p           = challenge["parameters"]
        nonce_bytes = bytes.fromhex(p["nonce"])
        salt        = bytes.fromhex(p["salt"])
        cost        = p["cost"]
        key_len     = p["keyLength"]
        key_prefix  = p["keyPrefix"]

        num_workers = max(1, (os.cpu_count() or 4) // 2)
        found       = threading.Event()
        result: list = [None]
        t0          = time.time()

        def _worker(start: int, step: int) -> None:
            counter = start
            while not found.is_set():
                password = nonce_bytes + counter.to_bytes(4, "big")
                dk       = hashlib.pbkdf2_hmac("sha256", password, salt, cost, dklen=key_len)
                hex_key  = binascii.hexlify(dk).decode()
                if hex_key.startswith(key_prefix):
                    result[0] = (counter, hex_key)
                    found.set()
                    return
                counter += step

        threads = [
            threading.Thread(target=_worker, args=(i, num_workers), daemon=True)
            for i in range(num_workers)
        ]
        for t in threads:
            t.start()
        found.wait()

        counter, hex_key = result[0]
        return {
            "counter":    counter,
            "derivedKey": hex_key,
            "time":       round((time.time() - t0) * 1000, 1),
        }

    def _get_squid_token(self, force_refresh: bool = False) -> str:
        if self._squid_token and not force_refresh:
            self._start_prefetch_if_needed()
            return self._squid_token

        _h = {
            "accept":       "*/*",
            "content-type": "application/json",
            "origin":       "https://amz.squid.wtf",
            "referer":      "https://amz.squid.wtf/",
            "user-agent":   _SQUID_UA,
        }
        try:
            challenge = self._session.get(
                f"{_SQUID_BASE}/captcha/challenge", headers=_h, timeout=15
            ).json()
            solution  = self._solve_pow(challenge)
            encoded   = base64.b64encode(
                json.dumps({"challenge": challenge, "solution": solution},
                           separators=(",", ":")).encode()
            ).decode()
            resp = self._session.post(
                f"{_SQUID_BASE}/captcha/verify",
                json={"payload": encoded}, headers=_h, timeout=15,
            )
            self._squid_token = resp.json()["token"]
            logger.info(
                "[amazon] Squid captcha OK — counter=%d, pow=%.0fms",
                solution["counter"], solution["time"],
            )
            return self._squid_token
        except Exception as exc:
            raise RuntimeError(f"[amazon] Squid captcha failed: {exc}") from exc

    def _prefetch_squid_token(self) -> None:
        try:
            self._squid_token = None
            self._get_squid_token()
        except Exception as exc:
            logger.debug("[amazon] Squid pre-fetch failed (non-blocking): %s", exc)

    def _start_prefetch_if_needed(self) -> None:
        t = self.__class__._prefetch_thread
        if t is None or not t.is_alive():
            self.__class__._prefetch_thread = threading.Thread(
                target=self._prefetch_squid_token, daemon=True
            )
            self.__class__._prefetch_thread.start()

    def _download_from_squid_api(self, asin: str, output_dir: str, requested_quality: str) -> tuple[str, dict] | None:
        """
        Download a track directly from amz.squid.wtf via GET /api/stream.
        Handles both native FLAC responses and M4A containers with FLAC stream inside.
        """
        logger.info("[amazon] Trying Squid API (ASIN: %s)", asin)

        _h = {
            "accept":       "*/*",
            "content-type": "application/json",
            "origin":       "https://amz.squid.wtf",
            "referer":      "https://amz.squid.wtf/",
            "user-agent":   _SQUID_UA,
        }

        q_str = str(requested_quality).lower().strip()
        tier  = "best" if q_str in ["hi_res", "hires", "hi-res", "hi-res-lossless"] else "hd"

        params    = {"asin": asin, "country": "US", "tier": tier}
        temp_file = os.path.join(output_dir, f"{asin}_squid.tmp")

        def _cleanup() -> None:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

        max_attempts = 2
        base_delay_s = 1.0

        for attempt in range(max_attempts):
            try:
                token = self._get_squid_token(force_refresh=(attempt > 0))
                _h["x-captcha-token"] = token

                with self._session.stream(
                    "GET",
                    f"{_SQUID_BASE}/stream",
                    params=params,
                    headers=_h,
                    timeout=120,
                ) as resp:

                    if resp.status_code in (401, 403) and attempt < max_attempts - 1:
                        logger.info("[amazon] Squid token rejected, refreshing…")
                        self._squid_token = None
                        time.sleep(base_delay_s)
                        continue

                    if resp.status_code != 200:
                        logger.warning("[amazon] Squid API returned HTTP %d", resp.status_code)
                        return None

                    total        = int(resp.headers.get("content-length", 0))
                    written      = 0
                    detected_ext: str | None = None
                    format_error = False

                    with open(temp_file, "wb") as f:
                        for chunk in resp.iter_bytes(65536):
                            if detected_ext is None:
                                if len(chunk) >= 4 and chunk[:4] == b"fLaC":
                                    detected_ext = ".flac"
                                elif len(chunk) >= 8 and chunk[4:8] == b"ftyp":
                                    detected_ext = ".m4a"
                                    logger.info("[amazon] Squid stream is M4A container (will demux if FLAC inside)")
                                else:
                                    logger.warning(
                                        "[amazon] Squid response is unrecognized format (magic=%s)",
                                        chunk[:min(8, len(chunk))].hex(),
                                    )
                                    format_error = True
                                    break
                            f.write(chunk)
                            written += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(written, total)

                    if format_error:
                        _cleanup()
                        return None

                # Rename to correct extension
                final_file = os.path.join(output_dir, f"{asin}_squid{detected_ext}")
                if os.path.exists(final_file):
                    os.remove(final_file)
                os.rename(temp_file, final_file)

                # If M4A, check inner codec — demux to FLAC if it's FLAC inside (like the JS client does)
                if detected_ext == ".m4a":
                    inner_codec = self._get_codec(final_file)
                    if inner_codec == "flac":
                        flac_out = os.path.join(output_dir, f"{asin}_squid.flac")
                        si = None
                        if os.name == "nt":
                            si = subprocess.STARTUPINFO()
                            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        result = subprocess.run(
                            [_ffmpeg_path(), "-y", "-i", final_file, "-c", "copy", flac_out],
                            capture_output=True, startupinfo=si,
                        )
                        if result.returncode == 0 and os.path.exists(flac_out):
                            os.remove(final_file)
                            final_file = flac_out
                            logger.info("[amazon] Squid: demuxed FLAC stream from M4A container")
                        else:
                            logger.warning("[amazon] Squid: FLAC demux failed, keeping M4A")

                logger.info("[amazon] Squid download complete — %.1f MB (%s)",
                            written / 1024 / 1024, os.path.splitext(final_file)[1])
                return final_file, {}

            except Exception as exc:
                logger.warning("[amazon] Squid error (attempt %d/%d): %s", attempt + 1, max_attempts, exc)
                _cleanup()
                if attempt < max_attempts - 1:
                    time.sleep(base_delay_s * (attempt + 1))
                    continue

        return None

    # ------------------------------------------------------------------
    # Download + Decrypt
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

    def _quality_to_zarz_codec(self, quality: str) -> str:
        """Map quality string to Amazon/Zarz codec name."""
        if not quality:
            return "flac"
        q = str(quality).lower().strip()
        if q in ["opus", "eac3", "mha1"]:
            return q
        if q == "dolby_atmos":
            return "eac3"
        return "flac"

    def _download_from_zarz_api(self, asin: str, output_dir: str, quality: str) -> tuple[str, dict] | None:
        codec = self._quality_to_zarz_codec(quality)
        logger.info("[amazon] Trying Zarz.moe API (ASIN: %s, codec: %s)", asin, codec)

        headers = {
            "Accept": "application/json",
            "User-Agent": "SpotiFLAC-Mobile/4.5.0"
        }

        def _fetch_zarz(target_codec: str):
            try:
                from ..core.http import zarz_rate_limiter
                zarz_rate_limiter.wait_for_slot()
            except ImportError:
                pass

            max_retries = 2
            base_delay = 3.0
            for attempt in range(max_retries):
                try:
                    r = self._make_api_request(
                        provider_key="zarz",
                        endpoint="/media",
                        headers=headers,
                        params={"asin": asin, "codec": target_codec}
                    )
                    if r.status_code == 429:
                        time.sleep(base_delay * (2 ** attempt))
                        continue
                    return r
                except (httpx.RequestError, httpx.ConnectError) as e:
                    logger.warning("[amazon] Zarz API connection error: %s", e)
                    break
            return None

        resp = _fetch_zarz(codec)

        if (not resp or resp.status_code != 200) and codec != "flac":
            logger.info("[amazon] Codec %s unavailable for ASIN: %s — falling back to FLAC", codec, asin)
            codec = "flac"
            resp = _fetch_zarz(codec)

        if not resp or resp.status_code != 200:
            status = resp.status_code if resp else "Connection Error"
            logger.warning("[amazon] Zarz API failed with status: %s", status)
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        if isinstance(data, list):
            if not data:
                return None
            data = data[0]

        audio          = data.get("audio", {})
        stream_url     = audio.get("url")
        decryption_key = audio.get("key", "").strip()
        returned_codec = audio.get("codec", codec)

        api_meta = {}
        m = data.get("meta", {})
        if m:
            api_meta = {
                "title":        m.get("title"),
                "artist":       m.get("artist"),
                "album":        m.get("album"),
                "album_artist": m.get("albumArtist"),
                "track_number": m.get("track"),
                "total_tracks": m.get("trackTotal"),
                "disc_number":  m.get("disc"),
                "total_discs":  m.get("discTotal"),
                "isrc":         m.get("isrc"),
                "genre":        m.get("genre"),
                "label":        m.get("label"),
                "copyright":    m.get("copyright"),
                "release_date": m.get("date"),
            }

        cover_url = data.get("cover", "")
        if cover_url:
            api_meta["cover_url"] = (
                cover_url
                .replace("{size}", "1200")
                .replace("{jpegQuality}", "94")
                .replace("{format}", "jpg")
            )

        if not stream_url:
            logger.warning("[amazon] No streamUrl in Zarz API response")
            return None

        temp_file = os.path.join(output_dir, f"{asin}_zarz.enc")
        logger.info("[amazon] Downloading encrypted stream from Zarz…")

        try:
            self._http.stream_to_file(stream_url, temp_file, self._progress_cb, extra_headers=headers)
        except Exception as exc:
            logger.warning("[amazon] Failed to download Zarz stream: %s", exc)
            if os.path.exists(temp_file):
                os.remove(temp_file)
            return None

        if returned_codec in ["eac3", "mha1", "opus"]:
            ext = ".mp4"
        else:
            ext = ".flac" if returned_codec == "flac" else ".m4a"

        if decryption_key:
            logger.info("[amazon] Decrypting Zarz stream…")
            out = os.path.join(output_dir, f"{asin}{ext}")

            si = None
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            result = subprocess.run(
                [_ffmpeg_path(), "-y", "-decryption_key", decryption_key,
                 "-i", temp_file, "-c", "copy", out],
                capture_output=True, startupinfo=si,
            )
            if os.path.exists(temp_file):
                os.remove(temp_file)

            if result.returncode != 0:
                logger.warning("[amazon] Zarz decryption failed: %s", result.stderr.decode()[:100])
                return None
            return out, api_meta

        final = os.path.join(output_dir, f"{asin}{ext}")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)
        return final, api_meta

    def _download_from_spotbye_api(self, asin: str, output_dir: str, provider_key: str) -> tuple[str, dict]:
        logger.info("[amazon] Fetching track from %s API (ASIN: %s)", provider_key, asin)

        config = API_ENDPOINTS.get(provider_key)
        method = config.get("method", "GET").upper()

        if method == "POST":
            endpoint = "/track"
            payload  = {"asin": asin, "tier": "best", "country": "US"}
            params   = None
            headers  = {"X-Debug-Key": _get_amazon_debug_key(), "Content-Type": "application/json"}
        else:
            endpoint = f"/track/{asin}"
            payload  = None
            params   = None
            headers  = {"X-Debug-Key": _get_amazon_debug_key()}

        try:
            resp = self._make_api_request(
                provider_key=provider_key, endpoint=endpoint,
                headers=headers, payload=payload, params=params,
            )
        except (httpx.RequestError, httpx.ConnectError) as exc:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"{provider_key} API request failed: {exc}",
                self.name,
            ) from exc

        if resp.status_code != 200:
            err_msg = resp.json() if "application/json" in resp.headers.get("Content-Type", "") else resp.text
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"{provider_key} API returned {resp.status_code}: {err_msg}",
                self.name,
            )

        data           = resp.json()
        api_meta       = data.get("metadata", {})
        stream_url     = data.get("streamUrl")
        decryption_key = data.get("decryptionKey")
        captcha_token  = data.get("x-captcha-token") or data.get("xCaptchaToken")

        if not stream_url:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"No streamUrl in {provider_key} API response",
                self.name,
            )

        temp_file = os.path.join(output_dir, f"{asin}.enc")
        download_headers = {}
        if captcha_token:
            download_headers["x-captcha-token"] = str(captcha_token)

        self._http.stream_to_file(stream_url, temp_file, self._progress_cb, extra_headers=download_headers)

        if decryption_key:
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

            if os.path.exists(temp_file):
                os.remove(temp_file)

            if result.returncode != 0:
                raise SpotiflacError(
                    ErrorKind.FILE_IO,
                    f"Decryption failed: {result.stderr.decode()[:100]}",
                    self.name,
                )
            return out, api_meta

        final = os.path.join(output_dir, f"{asin}.m4a")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)
        return final, api_meta

    def _download_from_spotbye1_api(self, asin: str, output_dir: str) -> tuple[str, dict]:
        resp = self._session.post(
            "https://amz.spotbye.qzz.io/api/track",
            json={"asin": asin, "tier": "best"},
            headers={"Accept": "*/*", "User-Agent": _DEFAULT_UA},
            timeout=30
        )

        if resp.status_code != 200:
            err_msg = resp.json() if "application/json" in resp.headers.get("Content-Type", "") else resp.text
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"spotbye1 API returned {resp.status_code}: {err_msg}",
                self.name,
            )

        data           = resp.json()
        api_meta       = data.get("metadata", {})
        stream_obj     = data.get("stream", {})
        drm_obj        = data.get("drm", {})

        stream_url     = stream_obj.get("url")
        decryption_key = drm_obj.get("key")
        captcha_token  = stream_obj.get("headers", {}).get("x-captcha-token")
        returned_codec = stream_obj.get("codec", "flac")

        if not stream_url or not captcha_token:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                "No streamUrl or captcha token in response",
                self.name,
            )

        stream_headers = {"User-Agent": _DEFAULT_UA, "x-captcha-token": captcha_token}
        temp_file = os.path.join(output_dir, f"{asin}.enc")

        self._http.stream_to_file(stream_url, temp_file, self._progress_cb, extra_headers=stream_headers)

        ext = ".flac" if returned_codec == "flac" else ".m4a"

        if decryption_key:
            out = os.path.join(output_dir, f"{asin}{ext}")
            si = None
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            result = subprocess.run(
                [_ffmpeg_path(), "-y", "-decryption_key", decryption_key.strip(),
                 "-i", temp_file, "-c", "copy", out],
                capture_output=True, startupinfo=si,
            )

            if os.path.exists(temp_file):
                os.remove(temp_file)

            if result.returncode != 0:
                raise SpotiflacError(
                    ErrorKind.FILE_IO,
                    f"Decryption failed: {result.stderr.decode()[:100]}",
                    self.name,
                )
            return out, api_meta

        final = os.path.join(output_dir, f"{asin}{ext}")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)
        return final, api_meta

    def _download_from_api(self, amazon_url: str, output_dir: str, quality: str) -> tuple[str, dict]:
        asin_match = re.search(r"(B[0-9A-Z]{9})", amazon_url)
        if not asin_match:
            raise RuntimeError(f"Cannot extract ASIN from: {amazon_url}")
        asin = asin_match.group(1)

        fallback_quality = str(quality).upper()

        # 1. ZARZ API (Primary)
        codec = self._quality_to_zarz_codec(quality)
        zarz_url = f"{API_ENDPOINTS['zarz']['base_url']}/media?asin={asin}&codec={codec}"
        display_quality = "Best Available Quality (up to 24-bit/48kHz)" if codec == "flac" else quality
        print_source_banner("amazon", zarz_url, display_quality)

        zarz_result = self._download_from_zarz_api(asin, output_dir, quality)
        if zarz_result and os.path.exists(zarz_result[0]):
            return zarz_result

        logger.info("[amazon] Zarz failed. Trying Squid API…")

        # 2. SQUID API (Fallback 1 — direct FLAC, no decryption)
        q_str       = str(quality).lower().strip()
        squid_tier  = "best" if q_str in ["hi_res", "hires", "hi-res", "hi-res-lossless", "hi_res_lossless" , "HI_RES", "HIRES", "HI-RES" ,"HI-RES-LOSSLESS", "HI_RES_LOSSLESS"] else "hd"
        print_source_banner("amazon", f"{_SQUID_BASE}/stream?asin={asin}&country=US&tier={squid_tier}", fallback_quality)
        try:
            squid_result = self._download_from_squid_api(asin, output_dir, quality)
            if squid_result and os.path.exists(squid_result[0]):
                return squid_result
        except Exception as exc:
            logger.warning("[amazon] Squid failed: %s", exc)

        logger.info("[amazon] Squid failed. Trying Spotbye1…")

        # 3. SPOTBYE 1 (Fallback 2)
        print_source_banner("amazon", API_ENDPOINTS['spotbye1']['base_url'], fallback_quality)
        try:
            return self._download_from_spotbye1_api(asin, output_dir)
        except Exception as exc:
            logger.warning("[amazon] Spotbye1 failed: %s", exc)

        logger.info("[amazon] Spotbye1 failed. Trying Spotbye2…")

        # 4. SPOTBYE 2 (Fallback 3)
        print_source_banner("amazon", API_ENDPOINTS['spotbye2']['base_url'], fallback_quality)
        try:
            return self._download_from_spotbye_api(asin, output_dir, provider_key="spotbye2")
        except SpotiflacError as exc:
            logger.warning("[amazon] Spotbye2 failed: %s", exc)
            raise
        except Exception as exc:
            logger.warning("[amazon] Spotbye2 unexpected failure: %s", exc)
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"spotbye2 API failed: {exc}",
                self.name,
            ) from exc

    # ------------------------------------------------------------------
    # Metadata Embedding
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
            api_metadata: dict | None = None,
    ) -> None:
        cover_data: bytes | None = None
        target_cover_url = (api_metadata and api_metadata.get("cover_url")) or cover_url
        target_cover_url = _fix_image_url(target_cover_url, size=1200)

        if target_cover_url:
            try:
                r = self._session.get(target_cover_url, timeout=15)
                if r.status_code == 200:
                    cover_data = r.content
            except Exception as exc:
                logger.warning("[amazon] Cover download failed: %s", exc)

        api_meta = api_metadata or {}

        t_title        = api_meta.get("title") or title
        t_artist       = api_meta.get("artist") or artist
        t_album        = api_meta.get("album") or album
        t_album_artist = api_meta.get("album_artist") or album_artist
        t_date         = api_meta.get("release_date") or date

        t_num   = _safe_int(api_meta.get("track_number") or track_num) or 1
        t_total = _safe_int(api_meta.get("total_tracks") or total_tracks) or 1
        d_num   = _safe_int(api_meta.get("disc_number") or disc_num) or 1
        d_total = _safe_int(api_meta.get("total_discs") or total_discs) or 1

        t_copy  = api_meta.get("copyright") or copyright
        t_label = api_meta.get("label") or publisher

        try:
            if filepath.endswith(".flac"):
                audio = FLAC(filepath)
                audio.delete()
                audio["TITLE"]       = t_title
                audio["ARTIST"]      = t_artist
                audio["ALBUM"]       = t_album
                audio["ALBUMARTIST"] = t_album_artist
                audio["DATE"]        = t_date
                audio["TRACKNUMBER"] = str(t_num)
                audio["TRACKTOTAL"]  = str(t_total)
                audio["DISCNUMBER"]  = str(d_num)
                audio["DISCTOTAL"]   = str(d_total)

                if t_copy:                    audio["COPYRIGHT"]    = t_copy
                if t_label:                   audio["ORGANIZATION"] = t_label
                if url:                       audio["URL"]          = url
                if api_meta.get("genre"):     audio["GENRE"]        = api_meta["genre"]
                if api_meta.get("composer"):  audio["COMPOSER"]     = api_meta["composer"]
                if api_meta.get("isrc"):      audio["ISRC"]         = api_meta["isrc"]
                if "is_explicit" in api_meta:
                    audio["ITUNESADVISORY"] = "1" if api_meta["is_explicit"] else "2"

                if cover_data:
                    pic      = Picture()
                    pic.data = cover_data
                    pic.type = PictureType.COVER_FRONT
                    pic.mime = "image/jpeg"
                    audio.add_picture(pic)
                audio.save()

            elif filepath.endswith((".m4a", ".mp4")):
                audio = MP4(filepath)
                audio.delete()
                audio["\xa9nam"] = t_title
                audio["\xa9ART"] = t_artist
                audio["\xa9alb"] = t_album
                audio["aART"]    = t_album_artist
                audio["\xa9day"] = t_date
                audio["trkn"]    = [(t_num, t_total)]
                audio["disk"]    = [(d_num, d_total)]

                if t_copy:                    audio["cprt"]                              = t_copy
                if api_meta.get("genre"):     audio["\xa9gen"]                           = api_meta["genre"]
                if api_meta.get("composer"):  audio["\xa9wrt"]                           = api_meta["composer"]
                if api_meta.get("isrc"):      audio["----:com.apple.iTunes:ISRC"]        = api_meta["isrc"].encode()
                if t_label:                   audio["----:com.apple.iTunes:LABEL"]       = t_label.encode()
                if "is_explicit" in api_meta:
                    audio["rtng"] = [2] if api_meta["is_explicit"] else [1]

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
            filename_format:     str              = "{title} - {artist}",
            position:            int              = 1,
            include_track_num:   bool             = False,
            use_album_track_num: bool             = False,
            first_artist_only:   bool             = False,
            allow_fallback:      bool             = True,
            quality:             str              = "LOSSLESS",
            embed_lyrics:        bool             = False,
            lyrics_providers:    list[str] | None = None,
            enrich_metadata:     bool             = False,
            enrich_providers:    list[str] | None = None,
            is_album:            bool             = False,
            **kwargs,
    ) -> DownloadResult:
        try:
            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.skipped_result(self.name, str(dest))

            from ..core.musicbrainz import AsyncMBFetch
            mb_fetcher = AsyncMBFetch(metadata.isrc) if getattr(metadata, "isrc", None) else None

            amazon_url = self._resolve_amazon_url(metadata)
            downloaded, api_metadata = self._download_from_api(amazon_url, output_dir, quality)

            ext      = os.path.splitext(downloaded)[1] or ".m4a"
            dest_ext = str(dest).rsplit(".", 1)[0] + ext

            if os.path.abspath(downloaded) != os.path.abspath(dest_ext):
                if os.path.exists(dest_ext):
                    os.remove(dest_ext)
                os.replace(downloaded, dest_ext)

            # ── MusicBrainz tags ─────────────────────────────────────
            mb_tags: dict[str, str] = {}
            res: dict = {}
            if mb_fetcher:
                res = mb_fetcher.future.result()

            mb_tags = mb_result_to_tags(res)

            if api_metadata:
                if api_metadata.get("genre"):      mb_tags["GENRE"]           = api_metadata["genre"]
                if api_metadata.get("label"):      mb_tags["LABEL"]           = api_metadata["label"]
                if api_metadata.get("isrc"):       mb_tags["ISRC"]            = api_metadata["isrc"]
                if api_metadata.get("composer"):   mb_tags["COMPOSER"]        = api_metadata["composer"]
                if api_metadata.get("copyright"):  mb_tags["COPYRIGHT"]       = api_metadata["copyright"]
                if "is_explicit" in api_metadata:
                    mb_tags["ITUNESADVISORY"] = "1" if api_metadata["is_explicit"] else "2"

            # ── Embedding ────────────────────────────────────────────
            if dest_ext.endswith(".flac"):
                opts = EmbedOptions(
                    first_artist_only = first_artist_only,
                    cover_url         = _fix_image_url(api_metadata.get("cover_url", metadata.cover_url)),
                    embed_lyrics      = embed_lyrics,
                    lyrics_providers  = lyrics_providers or [],
                    enrich            = enrich_metadata,
                    enrich_providers  = enrich_providers,
                    is_album          = is_album,
                    extra_tags        = mb_tags,
                )
                embed_metadata(dest_ext, metadata, opts, session=self._session)
            else:
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
                    api_metadata = api_metadata,
                )

            fmt = ext.replace(".", "")
            return DownloadResult.ok(self.name, dest_ext, fmt=fmt)

        except SpotiflacError as exc:
            logger.error("[amazon] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[amazon] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
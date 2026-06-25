from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
import concurrent.futures
from urllib.parse import urlparse

import aiofiles
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from mutagen.mp4 import MP4, MP4Cover

from ..core.console import print_source_banner
from ..core.endpoints import get_amazon_endpoint, get_pandora_base_and_path
from ..core.errors import ErrorKind, SpotiflacError
from ..core.flac_validation import validate_and_repair_if_needed
from ..core.isrc_utils import normalize_isrc
from ..core.models import DownloadResult, TrackMetadata
from ..core.musicbrainz import mb_result_to_tags, AsyncMBFetch
from ..core.quality import to_zarz_codec
from ..core.tagger import EmbedOptions, embed_metadata_async
from .base import BaseProvider
from .tidal import _find_isrc_via_qobuz

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_S_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

# Headers da usare per le richieste "community" verso mirror Amazon
_COMMUNITY_POST_HEADERS = {
    "User-Agent": "SpotiFLAC/7.1.9",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "x-api-key": "explore-obscure-chivalry-travesty-blinks",
}

# ---------------------------------------------------------------------------
# Backward Compatibility for Tagger
# ---------------------------------------------------------------------------


class _APIEndpointsProxy(dict):
    """
    Proxy dictionary to route legacy API_ENDPOINTS imports from tagger.py
    into the new get_amazon_endpoint registry system.
    """

    def __getitem__(self, key: str) -> str:
        return get_amazon_endpoint(key)

    def get(self, key: str, default=None):
        val = get_amazon_endpoint(key)
        return val if val else default


API_ENDPOINTS = _APIEndpointsProxy()

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

_AMAZON_DEBUG_KEY_SEED = b"spotif" + b"lac:am" + b"azon:spotbye:api:v1"
_AMAZON_DEBUG_KEY_AAD = bytes(
    [
        0x61,
        0x6D,
        0x61,
        0x7A,
        0x6F,
        0x6E,
        0x7C,
        0x73,
        0x70,
        0x6F,
        0x74,
        0x62,
        0x79,
        0x65,
        0x7C,
        0x64,
        0x65,
        0x62,
        0x75,
        0x67,
        0x7C,
        0x76,
        0x31,
    ]
)
_AMAZON_DEBUG_KEY_NONCE = bytes(
    [
        0x52,
        0x1F,
        0xA4,
        0x9C,
        0x13,
        0x77,
        0x5B,
        0xE2,
        0x81,
        0x44,
        0x90,
        0x6D,
    ]
)
_AMAZON_DEBUG_KEY_CIPHERTEXT_TAG = bytes(
    [
        0x5B,
        0xF9,
        0xC1,
        0x2E,
        0x58,
        0xF8,
        0x5B,
        0xC0,
        0x04,
        0x68,
        0x7E,
        0xFF,
        0x3D,
        0xD6,
        0x8B,
        0xE3,
        0x86,
        0x49,
        0x6C,
        0xFD,
        0xC1,
        0x49,
        0x0B,
        0xFB,
        0x6C,
        0x21,
        0x98,
        0x51,
        0xF2,
        0x38,
        0x4B,
        0x4A,
        0x23,
        0xE1,
        0xC6,
        0xD7,
        0x65,
        0x7F,
        0xFB,
        0xA1,
    ]
)

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
    cleaned = re.sub(r"\._[^.]+_\.", ".", url)
    if "images/I/" in cleaned or "images/S/" in cleaned:
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
    _prefetch_task: asyncio.Task | None = None

    def __init__(self, timeout_s: int = 120) -> None:
        # Pass the User-Agent to BaseProvider so the AsyncHttpClient inherits it!
        super().__init__(timeout_s=timeout_s, headers={"User-Agent": _DEFAULT_UA})
        self._s_token: str | None = None

    async def _make_api_request(
        self,
        provider_key: str,
        endpoint: str,
        headers: dict | None = None,
        params: dict | None = None,
        payload: dict | None = None,
        method: str = "GET",
    ) -> httpx.Response:
        base_url = get_amazon_endpoint(provider_key)
        if not base_url:
            raise ValueError(f"Endpoint not found for provider: {provider_key}")

        url = f"{base_url}{endpoint}"

        # Se questa base corrisponde all'endpoint community registrato, applica gli header specifici
        hdrs = headers or {}
        try:
            community_url = get_amazon_endpoint("community")
            if community_url and base_url.rstrip("/") == community_url.rstrip("/"):
                hdrs = {**hdrs, **_COMMUNITY_POST_HEADERS}
        except Exception:
            pass

        if method.upper() == "POST":
            return await self._async_http.post(
                url, json=payload, headers=hdrs, timeout=30
            )
        return await self._async_http.get(
            url, params=params, headers=hdrs, timeout=30
        )

    async def _do_request_with_retry(
        self,
        method: str,
        url: str,
        *,
        max_retries: int = 2,
        base_delay_s: float = 2.0,
        **kwargs,
    ) -> httpx.Response:
        retry_statuses = {429, 500, 502, 503, 504}
        client = await self._async_http._client()

        for attempt in range(max_retries):
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt < max_retries - 1:
                    logger.warning(
                        "[amazon] HTTP request error on attempt %d/%d: %s",
                        attempt + 1,
                        max_retries,
                        exc,
                    )
                    await asyncio.sleep(base_delay_s * (attempt + 1))
                    continue
                raise

            if response.status_code in retry_statuses:
                if attempt < max_retries - 1:
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = (
                                float(retry_after)
                                if retry_after
                                else base_delay_s * (attempt + 1)
                            )
                        except ValueError:
                            delay = base_delay_s * (attempt + 1)
                        delay = min(delay, 10.0)
                    else:
                        delay = base_delay_s * (attempt + 1)

                    logger.warning(
                        "[amazon] Retry %d/%d due to HTTP %d (%s %s)",
                        attempt + 1,
                        max_retries,
                        response.status_code,
                        method.upper(),
                        url,
                    )
                    await response.aclose()
                    await asyncio.sleep(delay)
                    continue

            return response
        return response

    # ------------------------------------------------------------------
    # Songlink / Fallback -> Amazon URL Resolver
    # ------------------------------------------------------------------

    def _format_amazon_url(self, raw_url: str) -> str:
        asin_match = re.search(r"([A-Z0-9]{10})", raw_url.upper())
        if not asin_match:
            raise RuntimeError(f"Failed to extract ASIN from resolved URL: {raw_url}")
        asin = asin_match.group(1)
        base = base64.b64decode("aHR0cHM6Ly9tdXNpYy5hbWF6b24uY29tL3RyYWNrcy8=").decode()
        return f"{base}{asin}?musicTerritory=US"

    def _extract_amazon_from_json_ld(self, obj) -> str | None:
        if isinstance(obj, list):
            for item in obj:
                res = self._extract_amazon_from_json_ld(item)
                if res:
                    return res
        elif isinstance(obj, dict):
            same_as = obj.get("sameAs", [])
            if isinstance(same_as, list):
                for link in same_as:
                    if isinstance(link, str) and "music.amazon." in link:
                        return link
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    res = self._extract_amazon_from_json_ld(v)
                    if res:
                        return res
        return None

    async def _resolve_via_songstats(self, isrc: str) -> str | None:
        url = f"https://songstats.com/{isrc.upper().strip()}?ref=ISRCFinder"
        try:
            resp = await self._async_http.get(
                url, headers={"Accept": "text/html"}, timeout=15
            )
            for match in re.finditer(
                r'<script type="application/ld\+json">([\s\S]*?)</script>', resp.text
            ):
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

    async def _resolve_amazon_url(self, metadata: TrackMetadata) -> str:
        """Multilayer resolver ported from JS checkAvailability/resolveAmazonURL"""
        if metadata.id and re.match(r"^B[0-9A-Z]{9}$", metadata.id.upper()):
            logger.info(f"[amazon] ID {metadata.id} is already an ASIN.")
            return self._format_amazon_url(
                f"https://music.amazon.com/tracks/{metadata.id}"
            )

        track_id = metadata.id

        # 1. ZARZ.MOE API (Spotify ID)
        source_url = f"https://open.spotify.com/track/{track_id}"
        try:
            _zarz_base, _zarz_path = get_pandora_base_and_path()
            if _zarz_base:
                _zarz_url = f"{_zarz_base.rstrip('/')}/v1/resolve"
                resp = await self._async_http.post(
                    _zarz_url,
                    json={"url": source_url},
                    headers={"User-Agent": "SpotiFLAC-Mobile/4.3.0"},
                    timeout=15,
                )
                data = resp.json()
                if data.get("success") and "AmazonMusic" in data.get("songUrls", {}):
                    amz_val = data["songUrls"]["AmazonMusic"]
                    amazon_url = (
                        amz_val[0] if isinstance(amz_val, list) and amz_val else amz_val
                    )
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
                resp = await self._async_http.get(sl_api_url, timeout=15)
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
            resp = await self._async_http.get(sl_url, timeout=15)
            asin_match = re.search(r"trackAsin=([A-Z0-9]{10})", resp.text)
            if not asin_match:
                asin_match = re.search(
                    r"https://music\.amazon\.com/tracks/([A-Z0-9]{10})", resp.text
                )
            if asin_match:
                logger.info("[amazon] Resolved via Songlink HTML Scraping")
                return self._format_amazon_url(asin_match.group(1))
        except Exception as exc:
            logger.warning(f"[amazon] Songlink HTML failed: {exc}")

        # 4. SONGLINK API (Spotify ID)
        try:
            sl_api_url = f"https://api.song.link/v1-alpha.1/links?url={source_url}&userCountry=US"
            resp = await self._async_http.get(sl_api_url, timeout=15)
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
                sl_api_url = (
                    f"https://api.song.link/v1-alpha.1/links?isrc={isrc}&userCountry=US"
                )
                resp = await self._async_http.get(sl_api_url, timeout=15)
                data = resp.json()
                links = data.get("linksByPlatform", {})
                if "amazonMusic" in links:
                    logger.info("[amazon] Resolved via SongLink API (ISRC)")
                    return self._format_amazon_url(links["amazonMusic"].get("url"))
            except Exception as exc:
                logger.warning(f"[amazon] SongLink API (ISRC) failed: {exc}")

            amz_url = await self._resolve_via_songstats(isrc)
            if amz_url:
                return self._format_amazon_url(amz_url)

        raise RuntimeError(
            f"Could not resolve Amazon URL for {track_id} via any method."
        )

    # ------------------------------------------------------------------
    # s PoW Captcha + Direct FLAC Download
    # ------------------------------------------------------------------

    async def _solve_pow(self, challenge: dict) -> dict:
        """Esegue il calcolo intensivo di hash in un thread asincrono worker separato, liberando l'event loop."""
        return await asyncio.to_thread(self._solve_pow_sync, challenge)

    def _solve_pow_sync(self, challenge: dict) -> dict:
        p = challenge["parameters"]
        nonce_bytes = bytes.fromhex(p["nonce"])
        salt = bytes.fromhex(p["salt"])
        cost = p["cost"]
        key_len = p["keyLength"]
        key_prefix = p["keyPrefix"]

        num_workers = max(1, (os.cpu_count() or 4) // 2)
        found = threading.Event()
        result: list = [None]
        t0 = time.time()

        def _worker(start: int, step: int) -> None:
            counter = start
            while not found.is_set():
                password = nonce_bytes + counter.to_bytes(4, "big")
                dk = hashlib.pbkdf2_hmac("sha256", password, salt, cost, dklen=key_len)
                hex_key = binascii.hexlify(dk).decode()
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
            "counter": counter,
            "derivedKey": hex_key,
            "time": round((time.time() - t0) * 1000, 1),
        }

    async def _get_s_token(self, force_refresh: bool = False) -> str:
        if self._s_token and not force_refresh:
            self._start_prefetch_if_needed()
            return self._s_token

        # Recuperiamo gli endpoint esatti dal registro cifrato
        s_home_url = get_amazon_endpoint("s_home")
        s_challenge_url = get_amazon_endpoint("s_challenge")
        s_verify_url = get_amazon_endpoint("s_verify")

        if not all([s_home_url, s_challenge_url, s_verify_url]):
            raise RuntimeError("[amazon] s endpoints not fully configured in registry")

        # Derive origin and referer dynamically from the home URL
        parsed = urlparse(s_home_url)
        origin = (
            f"{parsed.scheme}://{parsed.netloc}"
            if parsed and parsed.scheme and parsed.netloc
            else ""
        )
        referer = f"{origin}/" if origin else ""

        headers_nav = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="149", "Chromium";v="149"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": _S_UA,
        }

        headers_api = {
            "accept": "*/*",
            "accept-language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "content-type": "application/json",
            "origin": origin,
            "referer": referer,
            "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="149", "Chromium";v="149"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": _S_UA,
        }

        try:
            # Step 1: Navighiamo sulla root per estrarre il token di sessione (webNonce) HTML
            resp_home = await self._async_http.get(
                s_home_url, headers=headers_nav, timeout=15
            )

            web_nonce = None
            match = re.search(r"window\.__AMZ_WEB\s*=\s*(\{.*?\});", resp_home.text)
            if match:
                try:
                    amz_web_data = json.loads(match.group(1))
                    web_nonce = amz_web_data.get("n")
                except json.JSONDecodeError:
                    pass

            if not web_nonce:
                match_fallback = re.search(r'"n"\s*:\s*"(b1\.[^"]+)"', resp_home.text)
                if match_fallback:
                    web_nonce = match_fallback.group(1)

            if not web_nonce:
                raise RuntimeError("Missing webNonce in HTML")

            # Step 2: GET Challenge API (Senza Content-Type)
            h_challenge = headers_api.copy()
            h_challenge.pop("content-type", None)

            resp_challenge = await self._async_http.get(
                s_challenge_url, headers=h_challenge, timeout=15
            )
            challenge = resp_challenge.json()

            # Step 3: Calcolo PoW
            solution = await self._solve_pow(challenge)
            encoded = base64.b64encode(
                json.dumps(
                    {"challenge": challenge, "solution": solution},
                    separators=(",", ":"),
                ).encode()
            ).decode()

            # Step 4: POST Verify
            verify_payload = {"payload": encoded, "webNonce": web_nonce}

            payload_bytes = json.dumps(verify_payload, separators=(",", ":")).encode(
                "utf-8"
            )

            resp = await self._async_http.post(
                s_verify_url,
                content=payload_bytes,
                headers=headers_api,
                timeout=15,
            )

            self._s_token = resp.json()["token"]
            logger.info(
                "[amazon] s captcha OK — counter=%d, pow=%.0fms",
                solution["counter"],
                solution["time"],
            )

            await asyncio.sleep(1.5)
            return self._s_token

        except Exception as exc:
            raise RuntimeError(f"[amazon] s captcha failed: {exc}") from exc

    async def _prefetch_s_token(self) -> None:
        try:
            self._s_token = None
            await self._get_s_token()
        except Exception as exc:
            logger.debug("[amazon] s pre-fetch failed (non-blocking): %s", exc)

    def _start_prefetch_if_needed(self) -> None:
        t = self.__class__._prefetch_task
        if t is None or t.done():
            self.__class__._prefetch_task = asyncio.create_task(
                self._prefetch_s_token()
            )

    async def _download_from_s_api(
        self, asin: str, output_dir: str, requested_quality: str
    ) -> tuple[str, dict] | None:
        logger.info("[amazon] Trying s API (ASIN: %s)", asin)

        s_stream_url = get_amazon_endpoint("s_stream")
        if not s_stream_url:
            logger.warning(
                "[amazon] s stream endpoint not configured; skipping s fallback."
            )
            return None

        parsed = urlparse(s_stream_url)
        origin = (
            f"{parsed.scheme}://{parsed.netloc}"
            if parsed and parsed.scheme and parsed.netloc
            else ""
        )
        referer = f"{origin}/" if origin else ""

        headers_api = {
            "accept": "*/*",
            "accept-language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "origin": origin,
            "referer": referer,
            "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="149", "Chromium";v="149"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": _S_UA,
        }

        q_str = str(requested_quality).lower().strip()
        tier = (
            "best"
            if q_str in ["hi_res", "hires", "hi-res", "hi-res-lossless"]
            else "hd"
        )

        params = {"asin": asin, "country": "US", "tier": tier}
        temp_file = os.path.join(output_dir, f"{asin}_s.tmp")

        def _cleanup() -> None:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

        max_attempts = 2
        base_delay_s = 1.0

        client = await self._async_http._client()

        for attempt in range(max_attempts):
            try:
                token = await self._get_s_token(force_refresh=(attempt > 0))
                if not token:
                    logger.warning("[amazon] No s token obtained; skipping attempt.")
                    return None

                h_stream = headers_api.copy()
                h_stream["x-captcha-token"] = token
                h_stream.pop("content-type", None)

                async with client.stream(
                    "GET",
                    s_stream_url,
                    params=params,
                    headers=h_stream,
                    timeout=120,
                ) as resp:

                    if resp.status_code in (401, 403) and attempt < max_attempts - 1:
                        logger.info("[amazon] s token rejected, refreshing…")
                        self._s_token = None
                        await asyncio.sleep(base_delay_s)
                        continue

                    if (
                        resp.status_code in (502, 503, 504)
                        and attempt < max_attempts - 1
                    ):
                        logger.warning(
                            "[amazon] s API returned HTTP %d, retrying after backoff…",
                            resp.status_code,
                        )
                        await asyncio.sleep(base_delay_s * (attempt + 1))
                        continue

                    if resp.status_code != 200:
                        logger.warning(
                            "[amazon] s API returned HTTP %d", resp.status_code
                        )
                        return None

                    total = int(resp.headers.get("content-length", 0))
                    written = 0
                    detected_ext: str | None = None
                    format_error = False

                    async with aiofiles.open(temp_file, "wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            if detected_ext is None:
                                if len(chunk) >= 4 and chunk[:4] == b"fLaC":
                                    detected_ext = ".flac"
                                elif len(chunk) >= 8 and chunk[4:8] == b"ftyp":
                                    detected_ext = ".m4a"
                                    logger.info(
                                        "[amazon] s stream is M4A container (will demux if FLAC inside)"
                                    )
                                else:
                                    logger.warning(
                                        "[amazon] s response is unrecognized format (magic=%s)",
                                        chunk[: min(8, len(chunk))].hex(),
                                    )
                                    format_error = True
                                    break
                            await f.write(chunk)
                            written += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(written, total)

                    if format_error:
                        _cleanup()
                        return None

                final_file = os.path.join(output_dir, f"{asin}_s{detected_ext}")
                if os.path.exists(final_file):
                    os.remove(final_file)
                os.rename(temp_file, final_file)

                if detected_ext == ".m4a":
                    inner_codec = await self._get_codec(final_file)
                    if inner_codec == "flac":
                        flac_out = os.path.join(output_dir, f"{asin}_s.flac")
                        proc = await asyncio.create_subprocess_exec(
                            _ffmpeg_path(),
                            "-y",
                            "-i",
                            final_file,
                            "-c",
                            "copy",
                            flac_out,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await proc.communicate()
                        if proc.returncode == 0 and os.path.exists(flac_out):
                            os.remove(final_file)
                            final_file = flac_out
                            logger.info(
                                "[amazon] s: demuxed FLAC stream from M4A container"
                            )
                        else:
                            logger.warning("[amazon] s: FLAC demux failed, keeping M4A")

                logger.info(
                    "[amazon] s download complete — %.1f MB (%s)",
                    written / 1024 / 1024,
                    os.path.splitext(final_file)[1],
                )

                if final_file.lower().endswith(".flac"):
                    success, repair_msg = await asyncio.to_thread(
                        validate_and_repair_if_needed, final_file
                    )
                    if not success:
                        logger.error(
                            "[amazon] FLAC file validation failed: %s", repair_msg
                        )
                        _cleanup()
                        return None
                    if repair_msg:
                        logger.info("[amazon] FLAC file repair status: %s", repair_msg)

                return final_file, {}

            except Exception as exc:
                logger.warning(
                    "[amazon] s error (attempt %d/%d): %s",
                    attempt + 1,
                    max_attempts,
                    exc,
                )
                _cleanup()
                if attempt < max_attempts - 1:
                    await asyncio.sleep(base_delay_s * (attempt + 1))
                    continue

        return None

    # ------------------------------------------------------------------
    # Download + Decrypt
    # ------------------------------------------------------------------

    async def _get_codec(self, filepath: str) -> str:
        try:
            cmd = [
                _ffprobe_path(),
                "-v",
                "quiet",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                filepath,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            return stdout.decode().strip()
        except Exception:
            return "m4a"

    def _quality_to_zarz_codec(self, quality: str) -> str:
        """Map quality string to Amazon/Zarz codec name (delegates to core.quality)."""
        return to_zarz_codec(quality)

    def _build_zarz_url(self, base_url: str) -> str:
        if not base_url:
            raise ValueError("Zarz endpoint not configured")

        url = base_url.rstrip("/")
        if url.endswith("/media") or "/v1/media" in url:
            return url
        if "/v1/dl/" in url:
            return url
        return f"{url}/media"

    async def _download_from_zarz_api(
        self, asin: str, output_dir: str, quality: str
    ) -> tuple[str, dict] | None:
        codec = self._quality_to_zarz_codec(quality)
        logger.info("[amazon] Trying Zarz.moe API (ASIN: %s, codec: %s)", asin, codec)

        headers = {"Accept": "application/json", "User-Agent": "SpotiFLAC-Mobile/4.5.0"}

        async def _fetch_zarz(target_codec: str):
            try:
                from ..core.http import async_zarz_rate_limiter

                await async_zarz_rate_limiter.wait_for_slot()
            except ImportError:
                pass

            zarz_endpoint = get_amazon_endpoint("zarz_media") or get_amazon_endpoint(
                "zarz"
            )
            if not zarz_endpoint:
                logger.warning(
                    "[amazon] Zarz media endpoint not configured; skipping Zarz API."
                )
                return None

            url = self._build_zarz_url(zarz_endpoint)
            try:
                return await self._do_request_with_retry(
                    "GET",
                    url,
                    headers=headers,
                    params={"asin": asin, "codec": target_codec},
                    timeout=30,
                    max_retries=1,
                    base_delay_s=2.0,
                )
            except (httpx.RequestError, httpx.ConnectError) as e:
                logger.warning("[amazon] Zarz API connection error: %s", e)
                return None

        resp = await _fetch_zarz(codec)

        if (not resp or resp.status_code != 200) and codec != "flac":
            logger.info(
                "[amazon] Codec %s unavailable for ASIN: %s — falling back to FLAC",
                codec,
                asin,
            )
            codec = "flac"
            resp = await _fetch_zarz(codec)

        if not resp or resp.status_code != 200:
            status = resp.status_code if resp else "Connection Error"
            if status == 429:
                logger.warning(
                    "[amazon] Zarz API rate-limited (HTTP 429) for ASIN %s — skipping to next provider",
                    asin,
                )
            else:
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

        audio = data.get("audio", {})
        stream_url = audio.get("url")
        decryption_key = audio.get("key", "").strip()
        returned_codec = audio.get("codec", codec)

        api_meta = {}
        m = data.get("meta", {})
        if m:
            api_meta = {
                "title": m.get("title"),
                "artist": m.get("artist"),
                "album": m.get("album"),
                "album_artist": m.get("albumArtist"),
                "track_number": m.get("track"),
                "total_tracks": m.get("trackTotal"),
                "disc_number": m.get("disc"),
                "total_discs": m.get("discTotal"),
                "isrc": m.get("isrc"),
                "genre": m.get("genre"),
                "label": m.get("label"),
                "copyright": m.get("copyright"),
                "release_date": m.get("date"),
            }

        cover_url = data.get("cover", "")
        if cover_url:
            api_meta["cover_url"] = (
                cover_url.replace("{size}", "1200")
                .replace("{jpegQuality}", "94")
                .replace("{format}", "jpg")
            )

        if not stream_url:
            logger.warning("[amazon] No streamUrl in Zarz API response")
            return None

        temp_file = os.path.join(output_dir, f"{asin}_zarz.enc")
        logger.info("[amazon] Downloading encrypted stream from Zarz…")

        try:
            await self._async_http.stream_to_file(
                url=stream_url,
                dest_path=temp_file,
                progress_cb=self._progress_cb,
                chunk_size=65536,
                extra_headers=headers,
            )
        except Exception as exc:
            logger.warning("[amazon] Failed to download Zarz stream: %s", exc)
            return None

        if returned_codec in ["eac3", "mha1", "opus"]:
            ext = ".mp4"
        else:
            ext = ".flac" if returned_codec == "flac" else ".m4a"

        if decryption_key:
            logger.info("[amazon] Decrypting Zarz stream…")
            out = os.path.join(output_dir, f"{asin}{ext}")

            proc = await asyncio.create_subprocess_exec(
                _ffmpeg_path(),
                "-y",
                "-decryption_key",
                decryption_key,
                "-i",
                temp_file,
                "-c",
                "copy",
                out,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            if os.path.exists(temp_file):
                os.remove(temp_file)

            if proc.returncode != 0:
                logger.warning(
                    "[amazon] Zarz decryption failed: %s",
                    proc.stderr.decode()[:100] if proc.stderr else "",
                )
                if os.path.exists(out):
                    os.remove(out)
                return None

            if ext == ".flac":
                success, repair_msg = await asyncio.to_thread(
                    validate_and_repair_if_needed, out
                )
                if not success:
                    logger.error("[amazon] FLAC file validation failed: %s", repair_msg)
                    if os.path.exists(out):
                        os.remove(out)
                    return None
                if repair_msg:
                    logger.info("[amazon] FLAC file repair status: %s", repair_msg)

            return out, api_meta

        final = os.path.join(output_dir, f"{asin}{ext}")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)
        return final, api_meta

    async def _download_from_spotbye_api(
        self, asin: str, output_dir: str, provider_key: str
    ) -> tuple[str, dict]:
        logger.info(
            "[amazon] Fetching track from %s API (ASIN: %s)", provider_key, asin
        )

        endpoint_url = get_amazon_endpoint(provider_key)

        if not endpoint_url:
            raise SpotiflacError(ErrorKind.NETWORK, f"Invalid endpoint: {provider_key}")

        method = "GET"

        if method == "POST":
            endpoint = "/track"
            payload = {"asin": asin, "tier": "best", "country": "US"}
            params = None
            headers = {
                "X-Debug-Key": _get_amazon_debug_key(),
                "Content-Type": "application/json",
            }
        else:
            endpoint = f"/track/{asin}"
            payload = None
            params = None
            headers = {"X-Debug-Key": _get_amazon_debug_key()}

        url = f"{endpoint_url.rstrip('/')}{endpoint}"
        try:
            request_kwargs = {
                "headers": headers,
                "timeout": 30,
            }
            if method == "POST":
                request_kwargs["json"] = payload
            elif params is not None:
                request_kwargs["params"] = params

            resp = await self._do_request_with_retry(
                method,
                url,
                **request_kwargs,
            )
        except (httpx.RequestError, httpx.ConnectError) as exc:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"{provider_key} API request failed: {exc}",
                self.name,
            ) from exc

        if resp.status_code != 200:
            err_msg = (
                resp.json()
                if "application/json" in resp.headers.get("Content-Type", "")
                else resp.text
            )
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"{provider_key} API returned {resp.status_code}: {err_msg}",
                self.name,
            )

        data = resp.json()
        api_meta = data.get("metadata", {})
        stream_url = data.get("streamUrl")
        decryption_key = data.get("decryptionKey")
        captcha_token = data.get("x-captcha-token") or data.get("xCaptchaToken")

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

        try:
            await self._async_http.stream_to_file(
                url=stream_url,
                dest_path=temp_file,
                progress_cb=self._progress_cb,
                chunk_size=65536,
                extra_headers=download_headers,
            )
        except Exception as exc:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"Failed to download stream from {provider_key}: {exc}",
                self.name,
            )

        if decryption_key:
            codec = await self._get_codec(temp_file)
            ext = ".flac" if codec == "flac" else ".m4a"
            out = os.path.join(output_dir, f"{asin}{ext}")

            proc = await asyncio.create_subprocess_exec(
                _ffmpeg_path(),
                "-y",
                "-decryption_key",
                decryption_key.strip(),
                "-i",
                temp_file,
                "-c",
                "copy",
                out,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            if os.path.exists(temp_file):
                os.remove(temp_file)

            if proc.returncode != 0:
                raise SpotiflacError(
                    ErrorKind.FILE_IO,
                    f"Decryption failed: {proc.stderr.decode()[:100] if proc.stderr else ''}",
                    self.name,
                )

            if ext == ".flac":
                success, repair_msg = await asyncio.to_thread(
                    validate_and_repair_if_needed, out
                )
                if not success:
                    logger.error("[amazon] FLAC file validation failed: %s", repair_msg)
                    if os.path.exists(out):
                        os.remove(out)
                    raise SpotiflacError(
                        ErrorKind.FILE_IO,
                        f"FLAC validation failed: {repair_msg}",
                        self.name,
                    )
                if repair_msg:
                    logger.info("[amazon] FLAC file repair status: %s", repair_msg)

            return out, api_meta

        final = os.path.join(output_dir, f"{asin}.m4a")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)
        return final, api_meta

    async def _download_from_spotbye1_api(
        self, asin: str, output_dir: str
    ) -> tuple[str, dict]:
        base_url = get_amazon_endpoint("spotbye1")
        resp = await self._do_request_with_retry(
            "POST",
            f"{base_url}/track",
            json={"asin": asin, "tier": "best"},
            headers={"Accept": "*/*", "User-Agent": _DEFAULT_UA},
            timeout=30,
        )

        if resp.status_code != 200:
            err_msg = (
                resp.json()
                if "application/json" in resp.headers.get("Content-Type", "")
                else resp.text
            )
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"spotbye1 API returned {resp.status_code}: {err_msg}",
                self.name,
            )

        data = resp.json()
        api_meta = data.get("metadata", {})
        stream_obj = data.get("stream", {})
        drm_obj = data.get("drm", {})

        stream_url = stream_obj.get("url")
        decryption_key = drm_obj.get("key")
        captcha_token = stream_obj.get("headers", {}).get("x-captcha-token")
        returned_codec = stream_obj.get("codec", "flac")

        if not stream_url or not captcha_token:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                "No streamUrl or captcha token in response",
                self.name,
            )

        stream_headers = {"User-Agent": _DEFAULT_UA, "x-captcha-token": captcha_token}
        temp_file = os.path.join(output_dir, f"{asin}.enc")

        try:
            await self._async_http.stream_to_file(
                url=stream_url,
                dest_path=temp_file,
                progress_cb=self._progress_cb,
                chunk_size=65536,
                extra_headers=stream_headers,
            )
        except Exception as exc:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"Failed to download stream from spotbye1: {exc}",
                self.name,
            )

        ext = ".flac" if returned_codec == "flac" else ".m4a"

        if decryption_key:
            out = os.path.join(output_dir, f"{asin}{ext}")

            proc = await asyncio.create_subprocess_exec(
                _ffmpeg_path(),
                "-y",
                "-decryption_key",
                decryption_key.strip(),
                "-i",
                temp_file,
                "-c",
                "copy",
                out,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            if os.path.exists(temp_file):
                os.remove(temp_file)

            if proc.returncode != 0:
                raise SpotiflacError(
                    ErrorKind.FILE_IO,
                    f"Decryption failed: {proc.stderr.decode()[:100] if proc.stderr else ''}",
                    self.name,
                )

            if ext == ".flac":
                success, repair_msg = await asyncio.to_thread(
                    validate_and_repair_if_needed, out
                )
                if not success:
                    logger.error("[amazon] FLAC file validation failed: %s", repair_msg)
                    if os.path.exists(out):
                        os.remove(out)
                    raise SpotiflacError(
                        ErrorKind.FILE_IO,
                        f"FLAC validation failed: {repair_msg}",
                        self.name,
                    )
                if repair_msg:
                    logger.info("[amazon] FLAC file repair status: %s", repair_msg)

            return out, api_meta

        final = os.path.join(output_dir, f"{asin}{ext}")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)

        if ext == ".flac":
            success, repair_msg = await asyncio.to_thread(
                validate_and_repair_if_needed, final
            )
            if not success:
                logger.error("[amazon] FLAC file validation failed: %s", repair_msg)
                if os.path.exists(final):
                    os.remove(final)
                raise SpotiflacError(
                    ErrorKind.FILE_IO,
                    f"FLAC validation failed: {repair_msg}",
                    self.name,
                )
            if repair_msg:
                logger.info("[amazon] FLAC file repair status: %s", repair_msg)

        return final, api_meta

    async def _download_from_musicdl_api(
        self, amazon_url: str, asin: str, output_dir: str
    ) -> tuple[str, dict]:
        """Scaricamento tramite Telegram Bot CDN (dl.musicdl.me)"""
        logger.info("[amazon] Trying MusicDL API (ASIN: %s)", asin)

        payload = {"url": amazon_url, "platform": "amazon"}

        try:
            _musicdl_url = get_amazon_endpoint("musicdl")
            if not _musicdl_url:
                raise SpotiflacError(
                    ErrorKind.UNAVAILABLE, "MusicDL endpoint not configured", self.name
                )
            resp = await self._do_request_with_retry(
                "POST",
                _musicdl_url,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": _DEFAULT_UA},
                timeout=65,
            )
        except (httpx.RequestError, httpx.ConnectError) as exc:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE, f"MusicDL API request failed: {exc}", self.name
            ) from exc

        if resp.status_code != 200:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"MusicDL API returned {resp.status_code}: {resp.text}",
                self.name,
            )

        data = resp.json()
        if not data.get("success") or not data.get("download_url"):
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"MusicDL API failed: {data.get('error')}",
                self.name,
            )

        stream_url = data["download_url"]
        temp_file = os.path.join(output_dir, f"{asin}_musicdl.tmp")

        logger.info("[amazon] MusicDL returned stream URL, downloading...")

        try:
            await self._async_http.stream_to_file(
                url=stream_url,
                dest_path=temp_file,
                progress_cb=self._progress_cb,
                chunk_size=65536,
            )
        except Exception as exc:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"Failed to download stream from MusicDL: {exc}",
                self.name,
            )

        codec = await self._get_codec(temp_file)
        ext = ".flac" if codec == "flac" else ".m4a"

        final = os.path.join(output_dir, f"{asin}{ext}")
        if os.path.exists(final):
            os.remove(final)
        os.rename(temp_file, final)

        api_meta = {}
        if data.get("title"):
            api_meta["title"] = data["title"]
        if data.get("artist"):
            api_meta["artist"] = data["artist"]

        return final, api_meta

    async def _download_from_api(
        self, amazon_url: str, output_dir: str, quality: str
    ) -> tuple[str, dict]:
        asin_match = re.search(r"(B[0-9A-Z]{9})", amazon_url)
        if not asin_match:
            raise RuntimeError(f"Cannot extract ASIN from: {amazon_url}")
        asin = asin_match.group(1)

        fallback_quality = str(quality).upper()

        antra_url = get_amazon_endpoint("antra")
        if antra_url:
            logger.info("[amazon] Attempting direct download via Antra server (ASIN: %s)", asin)
            try:
                antra_headers = {
                    "User-Agent": _DEFAULT_UA,
                    "X-API-Key": "ak_8e3f1a7c2b5d9e4f0a6c3b8d1e5f2a9c7b4d0e6f",
                    "api-key": "ak_8e3f1a7c2b5d9e4f0a6c3b8d1e5f2a9c7b4d0e6f"
                }
                antra_api_url = f"{antra_url.rstrip('/')}/api/track/{asin}"
                
                client = await self._async_http._client()
                resp = await client.get(antra_api_url, headers=antra_headers, timeout=20)
                
                if resp.status_code == 200:
                    data = resp.json()
                    stream_url = data.get("streamUrl")
                    decryption_key = data.get("decryptionKey")
                    
                    if stream_url:
                        temp_file = os.path.join(output_dir, f"{asin}_antra.enc")
                        await self._async_http.stream_to_file(
                            url=stream_url,
                            dest_path=temp_file,
                            progress_cb=self._progress_cb,
                            chunk_size=65536,
                            extra_headers={"User-Agent": _DEFAULT_UA}
                        )
                        
                        codec = await self._get_codec(temp_file)
                        ext = ".flac" if codec == "flac" else ".m4a"
                        out = os.path.join(output_dir, f"{asin}{ext}")
                        
                        if decryption_key:
                            logger.info("[amazon] Decrypting Antra stream...")
                            proc = await asyncio.create_subprocess_exec(
                                _ffmpeg_path(), "-y", "-decryption_key", decryption_key.strip(),
                                "-i", temp_file, "-c", "copy", out,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            )
                            await proc.communicate()
                            
                            if os.path.exists(temp_file):
                                os.remove(temp_file)
                                
                            if proc.returncode == 0:
                                if ext == ".flac":
                                    success, repair_msg = await asyncio.to_thread(
                                        validate_and_repair_if_needed, out
                                    )
                                    if success:
                                        return out, {}
                                    else:
                                        logger.warning("[amazon] Antra FLAC repair failed: %s", repair_msg)
                                else:
                                    return out, {}
                            else:
                                logger.warning("[amazon] Antra decryption failed.")
                        else:
                            os.rename(temp_file, out)
                            return out, {}
            except Exception as exc:
                logger.warning("[amazon] Antra stream failed: %s. Falling back to resolvers...", exc)

        _zarz_ep = get_amazon_endpoint("zarz")
        _s_ep = get_amazon_endpoint("s_stream")
        _spotbye1_ep = get_amazon_endpoint("spotbye1")
        _spotbye2_ep = get_amazon_endpoint("spotbye2")
        _musicdl_ep = get_amazon_endpoint("musicdl")
        if not any([_zarz_ep, _s_ep, _spotbye1_ep, _spotbye2_ep, _musicdl_ep]):
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                "No Amazon endpoints configured in registry",
                self.name,
            )

        # 1. ZARZ API (Primary)
        codec = self._quality_to_zarz_codec(quality)
        display_quality = (
            "Best Available Quality (up to 24-bit/48kHz)"
            if codec == "flac"
            else quality
        )
        print_source_banner("amazon", "", display_quality)

        zarz_result = await self._download_from_zarz_api(asin, output_dir, quality)
        if zarz_result and os.path.exists(zarz_result[0]):
            return zarz_result

        logger.info("[amazon] Zarz failed. Trying s API…")

        # 2. s API (Fallback 1)
        print_source_banner("amazon", "", fallback_quality)
        try:
            s_result = await self._download_from_s_api(asin, output_dir, quality)
            if s_result and os.path.exists(s_result[0]):
                return s_result
        except Exception as exc:
            logger.warning("[amazon] s failed: %s", exc)

        logger.info("[amazon] s failed. Trying Spotbye1…")

        # 3. SPOTBYE 1 (Fallback 2)
        print_source_banner("amazon", "", fallback_quality)
        try:
            return await self._download_from_spotbye1_api(asin, output_dir)
        except Exception as exc:
            logger.warning("[amazon] Spotbye1 failed: %s", exc)

        logger.info("[amazon] Spotbye1 failed. Trying Spotbye2…")

        # 4. SPOTBYE 2 (Fallback 3)
        print_source_banner("amazon", "", fallback_quality)
        try:
            return await self._download_from_spotbye_api(
                asin, output_dir, provider_key="spotbye2"
            )
        except Exception as exc:
            logger.warning("[amazon] Spotbye2 failed: %s", exc)

        logger.info("[amazon] Spotbye2 failed. Trying MusicDL API…")

        # 5. MUSICDL (Fallback 4 - Telegram CDN)
        print_source_banner(
            "amazon", "", "BEST QUALITY AVAILABLE (MOSTLY 16 bit 44.1 Hz)"
        )
        try:
            return await self._download_from_musicdl_api(amazon_url, asin, output_dir)
        except Exception as exc:
            logger.warning("[amazon] MusicDL failed: %s", exc)
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"All Amazon APIs (including MusicDL) failed. Last error: {exc}",
                self.name,
            ) from exc

    # ------------------------------------------------------------------
    # Metadata Embedding
    # ------------------------------------------------------------------

    async def _embed_metadata(
        self,
        filepath: str,
        title: str,
        artist: str,
        album: str,
        album_artist: str,
        date: str,
        track_num: int,
        total_tracks: int,
        disc_num: int,
        total_discs: int,
        cover_url: str,
        copyright: str = "",
        publisher: str = "",
        url: str = "",
        api_metadata: dict | None = None,
    ) -> None:
        cover_data: bytes | None = None
        target_cover_url = (api_metadata and api_metadata.get("cover_url")) or cover_url
        target_cover_url = _fix_image_url(target_cover_url, size=1200)

        if target_cover_url:
            try:
                r = await self._async_http.get(target_cover_url, timeout=15)
                cover_data = r.content
            except Exception as exc:
                logger.warning("[amazon] Cover download failed: %s", exc)

        api_meta = api_metadata or {}

        t_title = api_meta.get("title") or title
        t_artist = api_meta.get("artist") or artist
        t_album = api_meta.get("album") or album
        t_album_artist = api_meta.get("album_artist") or album_artist
        t_date = api_meta.get("release_date") or date

        t_num = _safe_int(api_meta.get("track_number") or track_num) or 1
        t_total = _safe_int(api_meta.get("total_tracks") or total_tracks) or 1
        d_num = _safe_int(api_meta.get("disc_number") or disc_num) or 1
        d_total = _safe_int(api_meta.get("total_discs") or total_discs) or 1

        t_copy = api_meta.get("copyright") or copyright
        t_label = api_meta.get("label") or publisher

        def _sync_write_tags():
            try:
                if filepath.endswith(".flac"):
                    audio = FLAC(filepath)
                    audio.delete()
                    audio["TITLE"] = t_title
                    audio["ARTIST"] = t_artist
                    audio["ALBUM"] = t_album
                    audio["ALBUMARTIST"] = t_album_artist
                    audio["DATE"] = t_date
                    audio["TRACKNUMBER"] = str(t_num)
                    audio["TRACKTOTAL"] = str(t_total)
                    audio["DISCNUMBER"] = str(d_num)
                    audio["DISCTOTAL"] = str(d_total)

                    if t_copy:
                        audio["COPYRIGHT"] = t_copy
                    if t_label:
                        audio["ORGANIZATION"] = t_label
                    if url:
                        audio["URL"] = url
                    if api_meta.get("genre"):
                        audio["GENRE"] = api_meta["genre"]
                    if api_meta.get("composer"):
                        audio["COMPOSER"] = api_meta["composer"]
                    if api_meta.get("isrc"):
                        isrc_v = normalize_isrc(api_meta.get("isrc"))
                        if isrc_v:
                            audio["ISRC"] = isrc_v
                    if "is_explicit" in api_meta:
                        audio["ITUNESADVISORY"] = (
                            "1" if api_meta["is_explicit"] else "2"
                        )

                    if cover_data:
                        pic = Picture()
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
                    audio["aART"] = t_album_artist
                    audio["\xa9day"] = t_date
                    audio["trkn"] = [(t_num, t_total)]
                    audio["disk"] = [(d_num, d_total)]

                    if t_copy:
                        audio["cprt"] = t_copy
                    if api_meta.get("genre"):
                        audio["\xa9gen"] = api_meta["genre"]
                    if api_meta.get("composer"):
                        audio["\xa9wrt"] = api_meta["composer"]
                    if api_meta.get("isrc"):
                        isrc_v = normalize_isrc(api_meta.get("isrc"))
                        if isrc_v:
                            audio["----:com.apple.iTunes:ISRC"] = isrc_v.encode()
                    if t_label:
                        audio["----:com.apple.iTunes:LABEL"] = t_label.encode()
                    if "is_explicit" in api_meta:
                        audio["rtng"] = [2] if api_meta["is_explicit"] else [1]

                    if cover_data:
                        audio["covr"] = [
                            MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)
                        ]
                    audio.save()

                logger.info(
                    "[amazon] Metadata embedded: %s", os.path.basename(filepath)
                )
            except Exception as exc:
                logger.warning("[amazon] embed_metadata failed: %s", exc)

        await asyncio.to_thread(_sync_write_tags)

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

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
        quality: str = "LOSSLESS",
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
        **kwargs,
    ) -> DownloadResult:
        try:
            dest = self._build_output_path(
                metadata,
                output_dir,
                filename_format,
                position,
                include_track_num,
                use_album_track_num,
                first_artist_only,
            )
            if await asyncio.to_thread(self._file_exists, dest):
                return DownloadResult.skipped_result(self.name, str(dest))

            # Start ISRC search on Qobuz in parallel to reduce latency
            try:
                duration_ms = int(
                    getattr(metadata, "duration_ms", 0)
                    or (int(getattr(metadata, "duration", 0)) * 1000)
                )
            except Exception:
                duration_ms = 0

            try:
                qobuz_task = asyncio.create_task(
                    _find_isrc_via_qobuz(
                        getattr(metadata, "title", ""),
                        getattr(metadata, "artists", ""),
                        duration_ms,
                    )
                )
            except Exception:
                qobuz_task = None

            amazon_url = await self._resolve_amazon_url(metadata)
            downloaded, api_metadata = await self._download_from_api(
                amazon_url, output_dir, quality
            )

            # Normalize the Qobuz result (gather/task may raise exceptions)
            qobuz_isrc = None
            if qobuz_task is not None:
                try:
                    res = await qobuz_task
                    if isinstance(res, BaseException):
                        logger.debug("[amazon] Qobuz ISRC lookup raised: %s", res)
                    else:
                        qobuz_isrc = res
                except Exception as exc:
                    logger.debug("[amazon] Qobuz ISRC lookup failed: %s", exc)

            # Qobuz has absolute priority over Amazon API: always consult it,
            # il suo ISRC sovrascrive quello originale se trovato.
            # If Qobuz does NOT find an ISRC, then validate the one from the Amazon API.
            if qobuz_isrc:
                try:
                    normalized = normalize_isrc(qobuz_isrc)
                    if normalized:
                        logger.info(
                            "[amazon] ISRC from Qobuz (preferred): %s", normalized
                        )
                        metadata.isrc = normalized
                        if api_metadata is None:
                            api_metadata = {}
                        api_metadata["isrc"] = normalized
                except Exception:
                    logger.debug(
                        "[amazon] Failed to normalize Qobuz ISRC: %s", qobuz_isrc
                    )
            elif (
                api_metadata
                and api_metadata.get("isrc")
                and api_metadata["isrc"] != metadata.isrc
            ):
                try:
                    from ..core.isrc_utils import confirm_isrc_with_qobuz_async

                    isrc_val = normalize_isrc(api_metadata["isrc"])
                    if isrc_val:
                        # Retrieve the real duration from metadata instead of passing 0
                        track_duration = (
                            getattr(metadata, "duration", 0)
                            or getattr(metadata, "duration_ms", 0) / 1000
                            or 0
                        )

                        ok, _ = await confirm_isrc_with_qobuz_async(
                            isrc_val,
                            metadata.title or "",
                            metadata.artists or "",
                            track_duration,
                        )
                        if ok:
                            logger.info(
                                "[amazon] Syncing metadata ISRC: %s -> %s",
                                metadata.isrc,
                                isrc_val,
                            )
                            metadata.isrc = isrc_val
                            api_metadata["isrc"] = isrc_val
                        else:
                            logger.warning(
                                "[amazon] Qobuz verification failed for ISRC: %s",
                                isrc_val,
                            )
                            api_metadata["isrc"] = metadata.isrc
                except Exception as e:
                    logger.error("[amazon] Error during Qobuz ISRC validation: %s", e)

            _isrc_for_mb = normalize_isrc(getattr(metadata, "isrc", None) or "")
            logger.debug("[amazon] ISRC at MB lookup: %r", _isrc_for_mb)
            mb_fetcher = AsyncMBFetch(_isrc_for_mb) if _isrc_for_mb else None

            if not mb_fetcher:
                logger.warning("[amazon] MusicBrainz skipped: no valid ISRC available")

            ext = os.path.splitext(downloaded)[1] or ".m4a"
            dest_ext = str(dest).rsplit(".", 1)[0] + ext

            if os.path.abspath(downloaded) != os.path.abspath(dest_ext):
                if os.path.exists(dest_ext):
                    os.remove(dest_ext)
                os.replace(downloaded, dest_ext)

            # ── MusicBrainz tags ─────────────────────────────────────
            mb_tags: dict[str, str] = {}
            res: dict = {}
            if mb_fetcher:
                try:
                    res = await asyncio.to_thread(
                        lambda: mb_fetcher.future.result(timeout=12)
                    )
                except concurrent.futures.TimeoutError:
                    logger.warning(
                        "[amazon] MusicBrainz timed out after 12s, skipping MB tags"
                    )
                    res = {}
                except Exception as exc:
                    logger.warning("[amazon] MusicBrainz error: %s", exc)
                    res = {}

            mb_tags = mb_result_to_tags(res)

            if api_metadata:
                if api_metadata.get("genre"):
                    mb_tags["GENRE"] = api_metadata["genre"]
                if api_metadata.get("label"):
                    mb_tags["LABEL"] = api_metadata["label"]
                if api_metadata.get("isrc"):
                    isrc_v = normalize_isrc(api_metadata.get("isrc"))
                    if isrc_v:
                        mb_tags["ISRC"] = isrc_v
                if api_metadata.get("composer"):
                    mb_tags["COMPOSER"] = api_metadata["composer"]
                if api_metadata.get("copyright"):
                    mb_tags["COPYRIGHT"] = api_metadata["copyright"]
                if "is_explicit" in api_metadata:
                    mb_tags["ITUNESADVISORY"] = (
                        "1" if api_metadata["is_explicit"] else "2"
                    )

            # ── Embedding ────────────────────────────────────────────
            if dest_ext.endswith(".flac"):
                opts = EmbedOptions(
                    first_artist_only=first_artist_only,
                    cover_url=_fix_image_url(
                        api_metadata.get("cover_url", metadata.cover_url)
                    ),
                    embed_lyrics=embed_lyrics,
                    lyrics_providers=lyrics_providers or [],
                    enrich=enrich_metadata,
                    enrich_providers=enrich_providers,
                    is_album=is_album,
                    extra_tags=mb_tags,
                )
                await embed_metadata_async(dest_ext, metadata, opts)
            else:
                track_num = position
                if use_album_track_num and _safe_int(metadata.track_number) > 0:
                    track_num = _safe_int(metadata.track_number)
                artist = (
                    _first_artist(metadata.artists)
                    if first_artist_only
                    else metadata.artists
                )
                album_artist = (
                    _first_artist(metadata.album_artist)
                    if first_artist_only
                    else metadata.album_artist
                )

                await self._embed_metadata(
                    filepath=dest_ext,
                    title=metadata.title,
                    artist=artist,
                    album=metadata.album,
                    album_artist=album_artist,
                    date=metadata.release_date,
                    track_num=track_num,
                    total_tracks=_safe_int(metadata.total_tracks),
                    disc_num=_safe_int(metadata.disc_number),
                    total_discs=_safe_int(metadata.total_discs),
                    cover_url=metadata.cover_url,
                    api_metadata=api_metadata,
                )

            fmt = ext.replace(".", "")
            return DownloadResult.ok(self.name, dest_ext, fmt=fmt)

        except SpotiflacError as exc:
            logger.error("[amazon] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[amazon] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

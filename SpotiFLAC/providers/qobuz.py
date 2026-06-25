from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..core.console import print_api_failure, print_source_banner
from ..core.download_validation import validate_downloaded_track_async
from ..core.endpoints import get_qobuz_endpoints
from ..core.errors import (ErrorKind, NetworkError, ParseError, SpotiflacError,
                           TrackNotFoundError)
from ..core.http import (AsyncHttpClient, NetworkManager, RetryConfig,
                         async_zarz_rate_limiter)
from ..core.models import DownloadResult, TrackMetadata
from ..core.musicbrainz import fetch_mb_metadata_async, mb_result_to_tags
from ..core.provider_stats import (prioritize_providers_async,
                                   record_failure_async, record_success_async)
from ..core.quality import map_musicdl_quality
from ..core.tagger import EmbedOptions, _print_mb_summary, embed_metadata_async
from .base import BaseProvider

logger = logging.getLogger(__name__)


def _shorten_api_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_BASE       = "https://www.qobuz.com/api.json/0.2"
_DEFAULT_APP_ID = "798273057"
_DEFAULT_APP_SECRET = "589be88e4538daea11f509d29e4a23b1"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
_ZARZ_USER_AGENT = "SpotiFLAC-Mobile/4.5.0"
_CREDS_TTL        = 24 * 3600
_PROBE_ISRC       = "USUM71703861"
_OPEN_URL         = "https://open.qobuz.com/track/"
_CREDS_CACHE_FILE = os.path.join(
    os.path.expanduser("~"), ".cache", "spotiflac", "qobuz-credentials.json"
)

_BUNDLE_RE    = re.compile(r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"')
_API_CONFIG_RE = re.compile(r'app_id:"(?P<app_id>\d{9})",app_secret:"(?P<app_secret>[a-f0-9]{32})"')
_IMAGE_SIZE_RE = re.compile(r"_\d+\.jpg$")

_STREAM_APIS: list[str]         = get_qobuz_endpoints("stream")
_QOBUZ_DL_: list[str]           = get_qobuz_endpoints("dl")
_POST_APIS: list[str]           = get_qobuz_endpoints("post")
_GDSTUDIO_APIS: list[str]       = get_qobuz_endpoints("gdstudio")
_WJHE_APIS: list[str]           = get_qobuz_endpoints("wjhe")
_flacdownloader_raw              = get_qobuz_endpoints("flacdownloader")
_FLACDOWNLOADER_APIS: list[str] = (
    [_flacdownloader_raw] if isinstance(_flacdownloader_raw, str) and _flacdownloader_raw
    else list(_flacdownloader_raw) if isinstance(_flacdownloader_raw, list)
    else []
)
_COMMUNITY_APIS: list[str] = []
try:
    _community_raw = get_qobuz_endpoints("community")
    if isinstance(_community_raw, str) and _community_raw:
        _COMMUNITY_APIS = [_community_raw]
    elif isinstance(_community_raw, list):
        _COMMUNITY_APIS = _community_raw
except Exception:
    pass

_QUALITY_FALLBACK: dict[str, list[str]] = {
    "27":              ["27", "7", "6"],
    "7":               ["7", "6"],
    "6":               ["6"],
    "5":               ["6"],
    "":                ["6"],
    "HI_RES_LOSSLESS": ["27", "7", "6"],
    "HI_RES":          ["7", "6"],
    "LOSSLESS":        ["6"],
    "HIGH":            ["6"],
    "NORMAL":          ["6"],
    "BEST":            ["6"],
}

_TIDAL_TO_QOBUZ_QUALITY: dict[str, str] = {
    "DOLBY_ATMOS":     "27",
    "HI_RES_LOSSLESS": "27",
    "HI_RES":          "7",
    "LOSSLESS":        "6",
    "HIGH":            "6",
    "LOW":             "6",
}

_API_TIMEOUT_S      = 8
_MAX_RETRIES_GET    = 1          
_MAX_RETRIES_POST   = 2          
_RETRY_BASE_DELAY_S = 1.0        
_RETRY_MAX_DELAY_S  = 16.0       
_RETRY_JITTER       = 0.25       


# ---------------------------------------------------------------------------
# Text Normalization & Scoring (Ported from index.js)
# ---------------------------------------------------------------------------

def _remove_diacritics(text: str) -> str:
    text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("utf-8")
    return text.replace("đ", "dj").replace("Đ", "dj").replace("ß", "ss").replace("ẞ", "ss").replace("æ", "ae").replace("Æ", "ae").replace("œ", "oe").replace("Œ", "oe")

def _normalize_search_text(text: str) -> str:
    if not text:
        return ""
    text = _remove_diacritics(text).lower()
    text = re.sub(r'&', ' and ', text)
    text = re.sub(r'[^\w\s]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def _score_track_candidate(query: str, track: dict) -> int:
    query_norm = _normalize_search_text(query)
    if not query_norm or not track:
        return 0

    title_norm = _normalize_search_text(track.get("title", ""))
    
    version = (track.get("version") or "").strip()
    title_text = track.get("title") or ""
    display_title = f"{title_text} ({version})" if version else title_text
    display_norm = _normalize_search_text(display_title)

    performer = (track.get("performer") or {}).get("name", "")
    album_artist = (track.get("album") or {}).get("artist", {}).get("name", "")
    artist_norm = _normalize_search_text(performer or album_artist)
    album_norm = _normalize_search_text((track.get("album") or {}).get("title", ""))
    
    score = 0

    if query_norm == title_norm or query_norm == display_norm:
        score += 1200
    elif (title_norm and query_norm in title_norm) or (display_norm and query_norm in display_norm):
        score += 420

    if artist_norm and artist_norm in query_norm: score += 180
    if album_norm and album_norm in query_norm: score += 100
    isrc_value = track.get("isrc") or ""
    if isinstance(isrc_value, str) and isrc_value.strip():
        score += 15
    if track.get("maximum_bit_depth", 0) >= 24: score += 10
    if track.get("maximum_sampling_rate", 0) >= 88.2: score += 10

    return score

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@dataclass
class QobuzCredentials:
    app_id:          str
    app_secret:      str
    source:          str   = "embedded-default"
    fetched_at:      float = field(default_factory=time.time)
    user_auth_token: str | None = None

    def is_fresh(self) -> bool:
        return bool(self.app_id) and bool(self.app_secret) and (time.time() - self.fetched_at) < _CREDS_TTL

    def to_dict(self) -> dict:
        return {
            "app_id":          self.app_id,
            "app_secret":      self.app_secret,
            "source":          self.source,
            "fetched_at_unix": int(self.fetched_at),
            "user_auth_token": self.user_auth_token,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QobuzCredentials":
        token = d.get("user_auth_token") or os.environ.get("QOBUZ_AUTH_TOKEN")
        return cls(
            app_id          = d.get("app_id", ""),
            app_secret      = d.get("app_secret", ""),
            source          = d.get("source", ""),
            fetched_at      = float(d.get("fetched_at_unix", 0)),
            user_auth_token = token,
        )

    @classmethod
    def default(cls) -> "QobuzCredentials":
        return cls(
            app_id=_DEFAULT_APP_ID, 
            app_secret=_DEFAULT_APP_SECRET, 
            source="embedded-default",
            user_auth_token=os.environ.get("QOBUZ_AUTH_TOKEN")
        )

def _load_cached_credentials() -> QobuzCredentials | None:
    try:
        with open(_CREDS_CACHE_FILE, "r", encoding="utf-8") as f:
            return QobuzCredentials.from_dict(json.load(f))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Failed to read Qobuz credentials cache: %s", exc)
        return None

def _save_cached_credentials(creds: QobuzCredentials) -> None:
    try:
        os.makedirs(os.path.dirname(_CREDS_CACHE_FILE), exist_ok=True)
        with open(_CREDS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(creds.to_dict(), f, indent=2)
    except Exception as exc:
        logger.warning("Failed to write Qobuz credentials cache: %s", exc)

async def _load_cached_credentials_async() -> QobuzCredentials | None:
    try:
        return await asyncio.to_thread(_load_cached_credentials)
    except Exception as exc:
        logger.warning("Failed to read Qobuz credentials cache async: %s", exc)
        return None

async def _save_cached_credentials_async(creds: QobuzCredentials) -> None:
    await asyncio.to_thread(_save_cached_credentials, creds)

async def _scrape_credentials_async() -> QobuzCredentials:
    headers = {"User-Agent": _DEFAULT_UA}
    client = await NetworkManager.get_async_client_safe()

    resp = await client.get(f"{_OPEN_URL}1", headers=headers, timeout=15)
    resp.raise_for_status()

    m = _BUNDLE_RE.search(resp.text)
    if not m:
        raise RuntimeError("Qobuz bundle URL not found in HTML")

    bundle_url = m.group(1)
    if bundle_url.startswith("/"):
        bundle_url = "https://open.qobuz.com" + bundle_url

    bundle = await client.get(bundle_url, headers=headers, timeout=30)
    bundle.raise_for_status()

    cm = _API_CONFIG_RE.search(bundle.text)
    if not cm:
        raise RuntimeError("app_id/app_secret not found in Qobuz bundle")

    return QobuzCredentials(
        app_id     = cm.group("app_id"),
        app_secret = cm.group("app_secret"),
        source     = bundle_url,
    )

# ---------------------------------------------------------------------------
# Signature & API Helpers
# ---------------------------------------------------------------------------
def _compute_signature(path: str, params: dict, timestamp: str, secret: str) -> str:
    normalized = path.strip("/").replace("/", "")
    excluded   = {"app_id", "request_ts", "request_sig"}
    payload    = normalized
    for key in sorted(k for k in params if k not in excluded):
        val = params[key]
        if isinstance(val, list):
            for v in val:
                payload += key + str(v)
        else:
            payload += key + str(val)
    payload += timestamp + secret
    return hashlib.md5(payload.encode("utf-8")).hexdigest()

def _build_stream_url(api_base: str, track_id: int, quality: str) -> str:
    if api_base in _QOBUZ_DL_:
        return f"{api_base}track_id={track_id}&quality={quality}"
    if api_base.endswith("="):
        return f"{api_base}{track_id}&quality={quality}"
    return f"{api_base}{track_id}?quality={quality}"

def _map_musicdl_quality(quality: str) -> str:
    return map_musicdl_quality(quality)

def _map_local_api_quality(quality: str) -> str:
    if quality in ("27", "DOLBY_ATMOS", "HI_RES_LOSSLESS", "DEFAULT"):
        return "hi96"
    elif quality in ("7", "HI_RES"):
        return "hi24"
    elif quality == "5":
        return "mp3"
    return "flac"

# ---------------------------------------------------------------------------
# Fetch logic for mixed APIs (GET / POST)
# ---------------------------------------------------------------------------
def _extract_stream_url_from_json(data: dict) -> str | None:
    _URL_KEYS = ("download_url", "url", "link", "u")
    for key in _URL_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    nested = data.get("data")
    if isinstance(nested, dict):
        for key in _URL_KEYS:
            val = nested.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None

def _extract_audio_quality_from_json(data: dict) -> tuple[int, int]:
    nested = data.get("data") or {}
    bit_depth   = int(data.get("bit_depth")   or nested.get("bit_depth")   or 0)
    sample_rate = int(data.get("sampling_rate") or nested.get("sampling_rate") or 0)
    if 0 < sample_rate < 1000:
        sample_rate = round(sample_rate * 1000)
    return bit_depth, sample_rate

def _backoff_delay(attempt: int, server_hint_s: float | None = None) -> float:
    if server_hint_s is not None:
        base = max(server_hint_s, _RETRY_BASE_DELAY_S)
    else:
        base = min(_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), _RETRY_MAX_DELAY_S)
    jitter = base * _RETRY_JITTER * (2 * random.random() - 1)
    return max(0.1, base + jitter)

def _parse_retry_after(resp: httpx.Response) -> float | None: 
    raw = resp.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        import datetime
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        secs = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, secs)
    except Exception:
        return None

_gdstudio_ts9_cache: dict[str, tuple[str, float]] = {}
_gdstudio_ts9_lock  = threading.Lock()

_fd_token_cache: dict[str, tuple[str, float]] = {}
_fd_token_lock  = threading.Lock()
_bad_stream_urls:      set[str]       = set()
_bad_stream_urls_lock: threading.Lock = threading.Lock()
_dns_failed_hosts:      set[str]       = set()
_dns_failed_hosts_lock: threading.Lock = threading.Lock()

def _get_fd_token(
        client:    httpx.Client,
        origin:    str,
        timeout_s: int,
) -> str:
    fd_headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Referer": f"{origin}/download",
        "Cookie": "csrftoken=IaFTROF6th29hXV3Q5KtVw1oelBIGBXS",
    }

    now = time.time()
    with _fd_token_lock:
        cached = _fd_token_cache.get(origin)
        if cached and (now - cached[1]) < 55.0:
            return cached[0]

    prep_resp = client.get(f"{origin}/prepare", headers=fd_headers, timeout=timeout_s)
    if prep_resp.status_code == 429:
        raise RuntimeError("rate limited (HTTP 429) su /prepare")
    if prep_resp.status_code != 200:
        raise RuntimeError(f"HTTP {prep_resp.status_code} su /prepare (fd)")

    t_token = prep_resp.json().get("t")
    if not t_token:
        raise RuntimeError("fd /prepare: no token 't'")

    with _fd_token_lock:
        _fd_token_cache[origin] = (t_token, now)

    return t_token

def _get_gdstudio_ts9(host: str) -> str:
    now = time.time()
    with _gdstudio_ts9_lock:
        cached = _gdstudio_ts9_cache.get(host)
        if cached and (now - cached[1]) < 5.0:
            return cached[0]
    try:
        client = NetworkManager.get_sync_client()
        r = client.get(f"https://{host}/time", timeout=5)
        if r.status_code == 200:
            ts = r.text.strip()
            if len(ts) >= 9:
                result = ts[:9]
                with _gdstudio_ts9_lock:
                    _gdstudio_ts9_cache[host] = (result, now)
                return result
    except Exception:
        pass
    return str(int(time.time() * 1000))[:9]

def _build_gdstudio_signature(host: str, track_id: str, ts9: str) -> str:
    version = "20260510"  
    escaped_track_id = quote(track_id).replace("+", "%20")
    base = f"{host}|{version}|{ts9}|{escaped_track_id}"
    return hashlib.md5(base.encode("utf-8")).hexdigest().upper()[-8:]

# ---------------------------------------------------------------------------
# QobuzProvider
# ---------------------------------------------------------------------------
class QobuzProvider(BaseProvider):
    name = "qobuz"
    _is_async = True

    def __init__(
            self,
            timeout_s: int = 30,
            qobuz_token: str | None = None,
            local_api_url: str | None = None,
    ) -> None:
        super().__init__(
            timeout_s = timeout_s,
            retry     = RetryConfig(max_attempts=2),
            headers   = {"User-Agent": _DEFAULT_UA, "Accept": "application/json"},
        )
        self._creds:      QobuzCredentials | None = None
        self._creds_lock = asyncio.Lock()
        self._qobuz_token = qobuz_token or os.environ.get("QOBUZ_AUTH_TOKEN")
        self._local_api_url = local_api_url or os.environ.get("QOBUZ_LOCAL_API_URL")

    async def _async_raw_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        client = await self._async_http._client()
        # Extract user-provided headers and timeout to avoid duplicates
        user_headers = kwargs.pop("headers", {})
        headers = {**self._async_http._headers, **user_headers}
        timeout = kwargs.pop("timeout", self._async_http._timeout)
        return await client.request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )

    async def _get_credentials_async(self, force_refresh: bool = False) -> QobuzCredentials:
        async with self._creds_lock:
            if not force_refresh and self._creds and self._creds.is_fresh():
                if self._qobuz_token and not self._creds.user_auth_token:
                    self._creds.user_auth_token = self._qobuz_token
                return self._creds

        disk = await _load_cached_credentials_async()
        if not force_refresh and disk and disk.is_fresh():
            self._creds = disk
            if self._qobuz_token and not self._creds.user_auth_token:
                self._creds.user_auth_token = self._qobuz_token
            return self._creds

        scraped: QobuzCredentials | None = None
        try:
            candidate = await _scrape_credentials_async()
            if await self._probe_credentials_async(candidate):
                scraped = candidate
                await _save_cached_credentials_async(scraped)
                logger.info("[qobuz] fresh credentials (app_id=%s)", scraped.app_id)
        except Exception as exc:
            logger.warning("[qobuz] credential refresh failed: %s", exc)

        async with self._creds_lock:
            if scraped:
                self._creds = scraped
            elif disk:
                self._creds = disk
            elif not self._creds:
                logger.warning("[qobuz] using embedded fallback credentials")
                self._creds = QobuzCredentials.default()

            if self._qobuz_token and not self._creds.user_auth_token:
                self._creds.user_auth_token = self._qobuz_token
            return self._creds

    async def _probe_credentials_async(self, creds: QobuzCredentials) -> bool:
        try:
            resp = await self._do_signed_get_async(
                "track/search",
                {"query": _PROBE_ISRC, "limit": "1"},
                creds,
            )
            return resp.json().get("tracks", {}).get("total", 0) > 0
        except Exception:
            return False

    async def _do_signed_get_async(
            self,
            path:               str,
            params:             dict,
            creds:              QobuzCredentials | None = None,
            force_refresh:      bool = False,
            use_fallback_token: bool = False,
            _depth:             int  = 0,
    ) -> httpx.Response:
        if creds is None:
            creds = await self._get_credentials_async(force_refresh=force_refresh)

        timestamp = str(int(time.time()))
        signature = _compute_signature(path, params, timestamp, creds.app_secret)
        req_params = {
            **params,
            "app_id":      creds.app_id,
            "request_ts":  timestamp,
            "request_sig": signature,
        }
        url     = f"{_API_BASE}/{path.strip('/')}"
        headers = {"X-App-Id": creds.app_id}
        if creds.user_auth_token and use_fallback_token:
            headers["X-User-Auth-Token"] = creds.user_auth_token

        resp = await self._async_raw_request("GET", url, params=req_params, headers=headers)

        if resp.status_code in (400, 401) and _depth < 2:
            if creds.user_auth_token and not use_fallback_token and not force_refresh:
                return await self._do_signed_get_async(
                    path, params, creds=creds,
                    force_refresh=False, use_fallback_token=True, _depth=_depth + 1,
                )
            if not force_refresh:
                return await self._do_signed_get_async(
                    path, params,
                    force_refresh=True, use_fallback_token=use_fallback_token,
                    _depth=_depth + 1,
                )
        return resp

    async def _get_gdstudio_ts9_async(self, host: str) -> str:
        try:
            client = await self._async_http._client()
            resp = await client.get(f"https://{host}/time", timeout=5)
            if resp.status_code == 200:
                ts = resp.text.strip()
                if len(ts) >= 9:
                    return ts[:9]
        except Exception:
            pass
        return str(int(time.time() * 1000))[:9]

    async def _get_fd_token_async(
            self,
            client:    AsyncHttpClient,
            origin:    str,
            headers:   dict,
            timeout_s: int,
    ) -> str:
        now = time.time()
        with _fd_token_lock:
            cached = _fd_token_cache.get(origin)
            if cached and (now - cached[1]) < 55.0:
                return cached[0]

        resp = await client.get(f"{origin}/prepare", headers=headers, timeout=timeout_s)
        if resp.status_code == 429:
            raise RuntimeError("rate limited (HTTP 429) su /prepare")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} su /prepare (fd)")

        t_token = resp.json().get("t")
        if not t_token:
            raise RuntimeError("fd /prepare: nessun token 't'")

        with _fd_token_lock:
            _fd_token_cache[origin] = (t_token, now)

        return t_token

    async def _fetch_stream_url_once_async(
            self,
            client:        AsyncHttpClient,
            api_base:      str,
            track_id:      int,
            quality:       str,
            timeout_s:     int = _API_TIMEOUT_S,
            local_api_url: str | None = None,
    ) -> str:
        api_cleaning = api_base.rstrip('/')
        is_local_api = bool(local_api_url) and api_cleaning == local_api_url.rstrip('/')

        is_zarz = "zarz.moe" in api_cleaning
        is_gdstudio = "gdstudio" in api_cleaning
        is_wjhe = "wjhe.top" in api_cleaning
        is_squid = "squid.wtf" in api_cleaning
        is_fd = "flacdownloader.com" in api_cleaning

        is_post = api_base in _POST_APIS or api_base in _COMMUNITY_APIS or is_zarz or is_gdstudio or is_fd
        max_retries = _MAX_RETRIES_POST if is_post else _MAX_RETRIES_GET

        headers = {
            "User-Agent": _ZARZ_USER_AGENT if is_zarz else _DEFAULT_UA,
            "Accept": "application/json"
        }
        last_err: Exception = RuntimeError("no attempts made")

        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = _backoff_delay(attempt)
                logger.debug(
                    "[qobuz] retry %d/%d for %s after %.2fs",
                    attempt, max_retries, api_base, delay,
                )
                await asyncio.sleep(delay)

            try:
                if is_local_api:
                    local_q = _map_local_api_quality(quality)
                    url = f"{api_cleaning}/download-url/{track_id}?quality={local_q}"
                    resp = await client.get(url, headers=headers, timeout=timeout_s)

                elif is_gdstudio:
                    host = urlparse(api_base).netloc
                    ts9 = await self._get_gdstudio_ts9_async(host)
                    br = "999" if quality in ("27", "7") else "740" if quality in ("", "6") else "320"
                    payload = {
                        "types": "url",
                        "id": str(track_id),
                        "source": "qobuz",
                        "br": br,
                        "s": _build_gdstudio_signature(host, str(track_id), ts9),
                    }
                    gdstudio_headers = {
                        "User-Agent": _DEFAULT_UA,
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Origin": f"https://{host}",
                        "Referer": f"https://{host}/",
                        "Accept": "application/json",
                    }
                    resp = await client.post(api_base, data=payload, headers=gdstudio_headers, timeout=timeout_s)

                elif is_wjhe:
                    q_map = {"27": 2000, "7": 2000, "6": 1000, "": 1000}
                    wjhe_q = q_map.get(quality, 1000)
                    wjhe_f = "flac"
                    url = f"{api_base}?ID={track_id}&quality={wjhe_q}&format={wjhe_f}"
                    resp = await client.get(url, headers=headers, timeout=timeout_s, follow_redirects=False)
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if loc and loc.startswith("http"):
                            return loc

                elif is_fd:
                    parsed_fd = urlparse(api_cleaning)
                    base_origin = f"{parsed_fd.scheme}://{parsed_fd.netloc}"
                    # The flacdownloader expects a cookie + referer on /prepare and
                    # an X-Dl-Token + Referer on the POST. Mirror common browsers.
                    prepare_headers = {
                        "Accept": "*/*",
                        "User-Agent": _DEFAULT_UA,
                        "Cookie": "csrftoken=laFTROF6th29hXV3Q5KtVw1oelBIGBXS",
                        "Referer": f"{base_origin}/download",
                    }
                    # Fetch token using the prepare headers
                    t_token = await self._get_fd_token_async(client, base_origin, prepare_headers, timeout_s)
                    fd_headers = {
                        "Accept": "application/json",
                        "User-Agent": _DEFAULT_UA,
                        "Referer": f"{base_origin}/download",
                        "X-Dl-Token": t_token,
                        "Cookie": "csrftoken=laFTROF6th29hXV3Q5KtVw1oelBIGBXS",
                        "Content-Type": "application/json",
                    }
                    fmt_map = {"27": 27, "7": 7, "6": 6, "5": 5,
                               "HI_RES_LOSSLESS": 27, "HI_RES": 7, "LOSSLESS": 6}
                    fmt_id = fmt_map.get(quality, 7)
                    payload_fd = {
                        "url": f"{_OPEN_URL}{track_id}",
                        "formatId": fmt_id,
                    }
                    # Some flacdownloader entries are a bare domain (https://flacdownloader.com).
                    # In that case the correct POST path is /qobuz-asset — otherwise use the full api_cleaning as provided.
                    parsed_fd_full = urlparse(api_cleaning)
                    if not parsed_fd_full.path or parsed_fd_full.path == "/":
                        post_url = api_cleaning.rstrip("/") + "/qobuz-asset"
                    else:
                        post_url = api_cleaning
                    resp = await client.post(post_url, json=payload_fd, headers=fd_headers, timeout=timeout_s)

                elif is_squid:
                    import base64
                    import struct
                    parsed = urlparse(api_base)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    squid_headers = {
                        "User-Agent": _DEFAULT_UA,
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                    current_ts = int(time.time() * 1000)
                    chal_resp = await client.get(
                        f"{origin}/api/altcha/challenge",
                        params={"ts": current_ts},
                        headers=squid_headers,
                        timeout=timeout_s,
                    )
                    chal_resp.raise_for_status()
                    challenge_json_str = chal_resp.text
                    challenge_data = json.loads(challenge_json_str)
                    params = challenge_data["parameters"]
                    salt_hex = params.get("salt", "")
                    nonce_hex = params["nonce"]
                    key_prefix = params["keyPrefix"]
                    algorithm = params.get("algorithm", "SHA-256")
                    cost = params.get("cost", 1)
                    key_length = params.get("keyLength", 32)
                    if algorithm != "SHA-256":
                        raise ValueError(f"Algoritmo ALTCHA non supportato: {algorithm}")
                    salt_bytes = bytes.fromhex(salt_hex) if salt_hex else b""
                    nonce_bytes = bytes.fromhex(nonce_hex)
                    start_time = time.time()
                    counter = 0
                    derived = b""
                    while True:
                        password = nonce_bytes + struct.pack(">I", counter)
                        data = password
                        for i in range(cost):
                            data = hashlib.sha256(data).digest()
                        derived = data[:key_length]
                        hex_digest = derived.hex()
                        if hex_digest.startswith(key_prefix):
                            break
                        counter += 1
                    elapsed = (time.time() - start_time) * 1000
                    min_elapsed = 160.0 + random.uniform(0, 20)
                    if elapsed < min_elapsed:
                        await asyncio.sleep((min_elapsed - elapsed) / 1000.0)
                    solution = {
                        "counter": counter,
                        "derivedKey": hex_digest,
                        "time": round(max(elapsed, min_elapsed), 1),
                    }
                    payload_json = f'{{"challenge":{challenge_json_str},"solution":{json.dumps(solution, separators=(",", ":"))}}}'
                    payload_b64 = base64.b64encode(payload_json.encode()).decode()
                    verify_resp = await client.post(
                        f"{origin}/api/altcha/verify",
                        json={"payload": payload_b64},
                        headers={
                            "Origin": origin,
                            "Referer": f"{origin}/",
                            **squid_headers,
                        },
                        timeout=timeout_s,
                    )
                    verify_resp.raise_for_status()
                    url = _build_stream_url(api_base, track_id, quality)
                    resp = await client.get(
                        url,
                        headers={
                            "Origin": origin,
                            "Referer": f"{origin}/",
                            **squid_headers,
                        },
                        timeout=timeout_s,
                    )

                elif is_post:
                    if is_zarz:
                        await async_zarz_rate_limiter.wait_for_slot()
                    payload = {
                        "quality": _map_musicdl_quality(quality),
                        "upload_to_r2": False,
                        "url": f"{_OPEN_URL}{track_id}",
                    }
                    post_headers = {"User-Agent": _ZARZ_USER_AGENT if is_zarz else _DEFAULT_UA}
                    # If this API is in the community list, adapt payload and headers
                    if api_base in _COMMUNITY_APIS:
                        # Community endpoints expect an 'id' numeric and quality like "24"/"16"
                        try:
                            # map quality: 27/7 -> "24", else "16"
                            community_quality = "24" if str(quality) in ("27", "7") else "16"
                            payload = {
                                "id": int(track_id),
                                "quality": int(community_quality) if community_quality.isdigit() else community_quality,
                                "upload_to_r2": False,
                            }
                        except Exception:
                            payload = {"id": track_id, "quality": _map_musicdl_quality(quality), "upload_to_r2": False}
                        # set headers required by community
                        post_headers = {
                            "User-Agent": f"SpotiFLAC/7.1.9",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "x-api-key": "explore-obscure-chivalry-travesty-blinks",
                        }

                    # Attempt request, refresh credentials and retry once on 400/401
                    resp = await client.post(api_base, json=payload, headers=post_headers, timeout=timeout_s)
                    if resp.status_code in (400, 401):
                        try:
                            await self._get_credentials_async(force_refresh=True)
                            # rebuild headers with refreshed creds
                            creds = await self._get_credentials_async()
                            if creds:
                                if creds.app_id:
                                    post_headers["X-App-Id"] = creds.app_id
                                if creds.user_auth_token:
                                    post_headers["X-User-Auth-Token"] = creds.user_auth_token
                            resp = await client.post(api_base, json=payload, headers=post_headers, timeout=timeout_s)
                        except Exception:
                            pass

                else:
                    url = _build_stream_url(api_base, track_id, quality)
                    resp = await client.get(url, headers=headers, timeout=timeout_s)

                if resp.status_code == 429:
                    retry_after = _parse_retry_after(resp)
                    wait = _backoff_delay(attempt + 1, retry_after)
                    last_err = RuntimeError("rate limited (HTTP 429)")
                    if attempt < max_retries:
                        await asyncio.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    continue
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")

                text = resp.text.strip()
                if not text:
                    last_err = RuntimeError("empty response body")
                    continue
                if text.startswith("<"):
                    raise RuntimeError("received HTML instead of JSON")

                try:
                    data = resp.json()
                except ValueError:
                    last_err = RuntimeError("invalid JSON in response")
                    continue

                if isinstance(data.get("error"), str) and data["error"].strip():
                    raise RuntimeError(data["error"].strip())
                if isinstance(data.get("detail"), str) and data["detail"].strip():
                    raise RuntimeError(data["detail"].strip())
                if data.get("success") is False:
                    msg = data.get("message", "api returned success=false")
                    raise RuntimeError(str(msg))

                stream = _extract_stream_url_from_json(data)
                if stream:
                    return stream

                last_err = RuntimeError("no download URL in response")

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_err = exc
                if isinstance(exc, httpx.ConnectError) and "nodename nor servname" in str(exc):
                    host = urlparse(api_base).netloc
                    with _dns_failed_hosts_lock:
                        _dns_failed_hosts.add(host)
                    break
                continue
            except RuntimeError:
                raise
            except Exception as exc:
                last_err = exc
                if not is_post:
                    break
                continue

        raise last_err

    async def _fetch_stream_url_parallel_async(
            self,
            client:        AsyncHttpClient,
            apis:          list[str],
            track_id:      int,
            quality:       str,
            timeout_s:     int = _API_TIMEOUT_S,
            local_api_url: str | None = None,
    ) -> tuple[str, str, str]:
        if not apis:
            raise SpotiflacError(ErrorKind.UNAVAILABLE, "no stream APIs configured", "qobuz")

        start = time.time()
        with _dns_failed_hosts_lock:
            dead = frozenset(_dns_failed_hosts)
        available_apis = [a for a in apis if urlparse(a).netloc not in dead]
        if not available_apis:
            available_apis = apis

        tasks = {
            asyncio.create_task(
                self._fetch_stream_url_once_async(client, api, track_id, quality, timeout_s, local_api_url)
            ): api for api in available_apis
        }
        errors: list[str] = []
        deadline = time.time() + timeout_s + 2

        try:
            while tasks:
                timeout = max(0.0, deadline - time.time())
                if timeout <= 0:
                    break

                done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED, timeout=timeout)
                if not done:
                    break

                for task in done:
                    api = tasks.pop(task)
                    try:
                        stream_url = task.result()
                        short_api = _shorten_api_url(api)
                        logger.debug("[qobuz] parallel: got URL from %s in %.2fs", short_api, time.time() - start)
                        for pending_task in pending:
                            pending_task.cancel()
                        await record_success_async("qobuz", api)
                        print_source_banner("qobuz", api, quality)
                        return api, stream_url, quality
                    except Exception as exc:
                        err_msg = str(exc)[:80]
                        short_api = _shorten_api_url(api)
                        errors.append(f"{short_api}: {err_msg}")
                        await record_failure_async("qobuz", api)
                        print_api_failure("qobuz", api, err_msg)
                        continue

            raise TimeoutError()
        except TimeoutError:
            errors.append("global timeout exceeded")
        finally:
            for task in tasks:
                task.cancel()

        logger.debug("[qobuz] All APIs failed details: %s", "; ".join(errors))
        raise SpotiflacError(ErrorKind.UNAVAILABLE, "All stream APIs failed", "qobuz")

    async def _search_by_isrc_async(self, isrc: str) -> dict | None:
        try:
            if isrc.startswith("qobuz_"):
                track_id = isrc.removeprefix("qobuz_")
                resp = await self._do_signed_get_async("track/get", {"track_id": track_id})
                if resp.status_code != 200:
                    self._raise_api_error(resp, "track/get")
                return resp.json()

            resp = await self._do_signed_get_async("track/search", {"query": isrc, "limit": "1"})
            if resp.status_code != 200:
                self._raise_api_error(resp, "track/search")

            body = resp.text
            if not body.strip():
                raise ParseError(self.name, "empty response from track/search")
            data = resp.json()
            items = data.get("tracks", {}).get("items", [])
            if not items:
                raise TrackNotFoundError(self.name, isrc)
            return items[0]
        except Exception as exc:
            logger.debug("[qobuz] async ISRC search failed: %s", exc)
            return None

    async def _search_by_text_async(self, title: str, artist: str) -> dict | None:
        query = f"{title} {artist}".strip()
        try:
            resp = await self._do_signed_get_async("track/search", {"query": query, "limit": "10"})
            if resp.status_code != 200:
                return None

            items = resp.json().get("tracks", {}).get("items", [])
            if not items:
                return None

            best_match = None
            best_score = 0
            for item in items:
                score = _score_track_candidate(query, item)
                if score > best_score:
                    best_score = score
                    best_match = item

            if not best_match or best_score < 400:
                album_resp = await self._do_signed_get_async("album/search", {"query": query, "limit": "5"})
                if album_resp.status_code == 200:
                    albums = album_resp.json().get("albums", {}).get("items", [])
                    for album in albums:
                        album_id = album.get("id")
                        if not album_id:
                            continue
                        track_resp = await self._do_signed_get_async("album/get", {"album_id": album_id})
                        if track_resp.status_code != 200:
                            continue
                        album_data = track_resp.json()
                        album_tracks = album_data.get("tracks", {}).get("items", [])
                        for trk in album_tracks:
                            trk["album"] = album_data
                            score = _score_track_candidate(query, trk)
                            if score > best_score:
                                best_score = score
                                best_match = trk

            return best_match
        except Exception as exc:
            logger.debug("[qobuz] async text search failed: %s", exc)
            return None

    async def _search_by_text_with_duration_async(self, title: str, artist: str, target_duration_s: int) -> dict | None:
        query = f"{title} {artist}".strip()
        try:
            resp = await self._do_signed_get_async("track/search", {"query": query, "limit": "20"})
            if resp.status_code != 200:
                return None
            items = resp.json().get("tracks", {}).get("items", [])
            if not items:
                return None

            best_match = None
            best_score = float("inf")
            for item in items:
                qobuz_dur = item.get("duration", 0)
                if qobuz_dur == 0:
                    continue
                diff = abs(qobuz_dur - target_duration_s)
                if diff < best_score:
                    best_score = diff
                    best_match = item
            if best_match and best_score <= 15:
                return best_match
            return None
        except Exception as exc:
            logger.debug("[qobuz] duration-aware async search failed: %s", exc)
            return None

    async def _get_stream_url_async(
            self,
            track_id: int,
            quality: str,
            allow_fallback: bool,
            exclude_apis: set[str] | None = None,
    ) -> tuple[str, str, str]:
        all_apis = list(_STREAM_APIS) + list(_QOBUZ_DL_) + list(_POST_APIS) + list(_COMMUNITY_APIS) + list(_GDSTUDIO_APIS) + list(_WJHE_APIS) + list(_FLACDOWNLOADER_APIS)
        ordered_apis = await prioritize_providers_async("qobuz", all_apis)
        if self._local_api_url:
            cleaned_local_api = self._local_api_url.rstrip('/')
            if cleaned_local_api in ordered_apis:
                ordered_apis.remove(cleaned_local_api)
            ordered_apis.insert(0, cleaned_local_api)
        ordered_apis = [api for api in ordered_apis if api not in (exclude_apis or set())]
        
        return await self._fetch_stream_url_parallel_async(
            self._async_http,
            ordered_apis,
            track_id,
            quality,
            timeout_s=_API_TIMEOUT_S,
            local_api_url=self._local_api_url,
        )

    async def _get_audio_duration_seconds_async(self, file_path: str) -> int:
        try:
            rc, stdout, _ = await self._run_ffprobe(
                "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", file_path,
            )
            return int(float(stdout.strip())) if stdout.strip() else 0
        except Exception:
            return 0

    @staticmethod
    def _raise_api_error(resp: httpx.Response, endpoint: str) -> None:
        try:
            msg = resp.json().get("message", f"HTTP {resp.status_code}")
        except Exception:
            msg = f"HTTP {resp.status_code}"
        raise NetworkError("qobuz", f"{endpoint} → {msg}")

    async def download_track_async(
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
            quality:             str  = "6",
            embed_genre:         bool = True,
            single_genre:        bool = True,
            embed_lyrics:        bool            = False,
            lyrics_providers:    list[str] | None = None,
            enrich_metadata:     bool = False,
            enrich_providers:    list[str] | None = None,
            is_album:            bool = False,
            **kwargs,
    ) -> DownloadResult:

        quality = _TIDAL_TO_QOBUZ_QUALITY.get(quality, quality)

        try:
            track = None
            if metadata.isrc:
                track = await self._search_by_isrc_async(metadata.isrc)

            if not track:
                logger.info("[qobuz] Trying textual search for: %s - %s", metadata.title, metadata.artists)
                track = await self._search_by_text_async(metadata.title, metadata.artists)

            if not track:
                raise TrackNotFoundError(self.name, f"Track not found (ISRC: {metadata.isrc}, Title: {metadata.title})")

            track_id = track.get("id")
            if not track_id:
                raise TrackNotFoundError(self.name, "Missing track ID in Qobuz response")

            if metadata.duration_ms > 0:
                qobuz_duration_s = track.get("duration", 0)
                expected_s = metadata.duration_ms // 1000
                if qobuz_duration_s > 0 and abs(qobuz_duration_s - expected_s) > 15:
                    logger.warning(
                        "[qobuz] track_id=%s has duration %ds, expected %ds — attempting duration-aware search",
                        track_id, qobuz_duration_s, expected_s,
                    )
                    alt_track = await self._search_by_text_with_duration_async(
                        metadata.title, metadata.artists, expected_s
                    )
                    if alt_track:
                        alt_duration = alt_track.get("duration", 0)
                        if abs(alt_duration - expected_s) < abs(qobuz_duration_s - expected_s):
                            logger.info("[qobuz] found alternative version: track_id=%s duration=%ds", alt_track.get("id"), alt_duration)
                            track = alt_track
                            track_id = track.get("id")

            album_data = track.get("album", {})
            images = album_data.get("image", {})
            qobuz_cover = images.get("large") or images.get("small")
            if qobuz_cover:
                metadata.cover_url = _IMAGE_SIZE_RE.sub("_max.jpg", qobuz_cover)

            metadata.release_date = track.get("release_date_original") or album_data.get("release_date_original") or metadata.release_date
            metadata.copyright = track.get("copyright") or album_data.get("copyright") or metadata.copyright

            composer_obj = track.get("composer")
            if composer_obj and composer_obj.get("name"):
                metadata.composer = composer_obj["name"]

            qobuz_extra_tags = {}
            if album_data.get("genre") and album_data["genre"].get("name"):
                qobuz_extra_tags["GENRE"] = album_data["genre"]["name"]
            if album_data.get("label") and album_data["label"].get("name"):
                qobuz_extra_tags["LABEL"] = album_data["label"]["name"]
                qobuz_extra_tags["ORGANIZATION"] = album_data["label"]["name"]
            if album_data.get("upc"):
                qobuz_extra_tags["BARCODE"] = album_data["upc"]
                qobuz_extra_tags["UPC"] = album_data["upc"]
            if album_data.get("maximum_technical_specifications"):
                qobuz_extra_tags["TECHNICAL_SPECIFICATIONS"] = album_data["maximum_technical_specifications"]
            if track.get("performers"):
                qobuz_extra_tags["COMMENT"] = track["performers"]
            if track.get("parental_warning"):
                qobuz_extra_tags["ITUNESADVISORY"] = "1"

            qobuz_track_id = str(track.get("id", ""))
            qobuz_album_id = str(album_data.get("qobuz_id", ""))
            if qobuz_track_id:
                qobuz_extra_tags["QOBUZ_TRACK_ID"] = qobuz_track_id
            if qobuz_album_id:
                qobuz_extra_tags["QOBUZ_ALBUM_ID"] = qobuz_album_id
            if album_data.get("url"):
                qobuz_extra_tags["URL"] = album_data["url"]
            if track.get("isrc"):
                try:
                    from ..core.isrc_utils import normalize_isrc
                    isrc_val = normalize_isrc(track["isrc"])
                    if isrc_val:
                        try:
                            from ..core.isrc_utils import \
                                confirm_isrc_with_qobuz_async
                            ok, _ = await confirm_isrc_with_qobuz_async(isrc_val, metadata.title or "", metadata.artists or "", metadata.duration_ms or 0)
                            if ok:
                                metadata.isrc = isrc_val
                        except Exception:
                            metadata.isrc = isrc_val
                except Exception:
                    metadata.isrc = ""
            if track.get("track_number"):
                metadata.track_number = track["track_number"]
            if album_data.get("tracks_count"):
                metadata.total_tracks = album_data["tracks_count"]

            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.skipped_result(self.name, str(dest))

            expected_s = metadata.duration_ms // 1000
            excluded_apis = set()
            valid = False
            last_err = None

            mb_tags: dict[str, str] = {}
            if metadata.isrc:
                mb_tags = mb_result_to_tags(await fetch_mb_metadata_async(metadata.isrc))

            mb_tags.update(qobuz_extra_tags)
            _print_mb_summary(mb_tags)

            while not valid:
                try:
                    winner_api, stream_url, used_quality = await self._get_stream_url_async(
                        track_id, quality, allow_fallback, exclude_apis=excluded_apis
                    )
                except SpotiflacError as exc:
                    if last_err:
                        raise SpotiflacError(
                            ErrorKind.UNAVAILABLE,
                            f"All APIs failed (errors or previews). Last reason: {last_err}",
                            self.name,
                        )
                    raise exc

                with _bad_stream_urls_lock:
                    is_bad_url = stream_url in _bad_stream_urls
                if is_bad_url:
                    logger.warning("[qobuz] stream URL already known-bad, blacklisting API and skipping download")
                    await record_failure_async("qobuz", winner_api)
                    excluded_apis.add(winner_api)
                    last_err = "stream URL already known-bad"
                    continue

                await self._async_http.stream_to_file(
                    stream_url,
                    str(dest),
                    self._progress_cb,
                    extra_headers={
                        "User-Agent":      _DEFAULT_UA,
                        "Accept":          "audio/flac, audio/*, */*",
                        "Accept-Encoding": "identity",
                        "Referer":         "https://open.qobuz.com/",
                        "Origin":          "https://open.qobuz.com",
                    },
                )
                valid, err = await validate_downloaded_track_async(str(dest), expected_s)
                if not valid:
                    actual_duration = await self._get_audio_duration_seconds_async(str(dest))
                    if actual_duration > 0 and actual_duration <= 35 and expected_s > 45:
                        err = "Preview-length audio detected (30s limit)"

                    logger.warning("[qobuz] stream API returned invalid file: %s. Blacklisting endpoint and retrying...", err)
                    with _bad_stream_urls_lock:
                        _bad_stream_urls.add(stream_url)
                    await record_failure_async("qobuz", winner_api)
                    excluded_apis.add(winner_api)
                    last_err = err

                    if os.path.exists(dest):
                        try:
                            os.remove(dest)
                            logger.debug("[qobuz] Cleaned up invalid file: %s", dest)
                        except OSError as e:
                            logger.error("[qobuz] Failed to remove invalid file: %s", e)
                    continue

                try:
                    opts = EmbedOptions(
                        first_artist_only       = first_artist_only,
                        cover_url               = metadata.cover_url,
                        extra_tags              = mb_tags,
                        embed_lyrics            = embed_lyrics,
                        lyrics_providers        = lyrics_providers or [],
                        enrich                  = enrich_metadata,
                        enrich_providers        = enrich_providers,
                        enrich_qobuz_token      = self._qobuz_token or "",
                        is_album                = is_album,
                    )
                    await embed_metadata_async(str(dest), metadata, opts)
                except SpotiflacError as exc:
                    message = str(exc).lower()
                    if exc.kind == ErrorKind.FILE_IO and "not a valid flac file" in message:
                        logger.warning(
                            "[qobuz] stream API returned invalid FLAC file: %s. Blacklisting endpoint and retrying...",
                            exc,
                        )
                        await record_failure_async("qobuz", winner_api)
                        excluded_apis.add(winner_api)
                        last_err = exc
                        if os.path.exists(dest):
                            try:
                                os.remove(dest)
                            except OSError as e:
                                logger.error("[qobuz] Failed to remove invalid file: %s", e)
                        continue
                    raise

                break

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[qobuz] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[qobuz] unexpected error")
            return DownloadResult.fail(self.name, f"unexpected: {exc}")
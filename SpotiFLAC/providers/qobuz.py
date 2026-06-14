from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from urllib.parse import urlparse, quote

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .base import BaseProvider
from ..core.console import (
    print_source_banner, print_api_failure, print_quality_fallback,
)
from ..core.download_validation import validate_downloaded_track
from ..core.errors import (
    TrackNotFoundError, NetworkError,
    ParseError, SpotiflacError, ErrorKind,
)
from ..core.http import RetryConfig, NetworkManager
from ..core.models import TrackMetadata, DownloadResult
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags
from ..core.provider_stats import record_success, record_failure, prioritize_providers
from ..core.tagger import _print_mb_summary, EmbedOptions
from ..core.tagger import embed_metadata

logger = logging.getLogger(__name__)

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

# Metadata API base URLs from manifest.json settings (primary + fallback).
_META_API_PRIMARY  = "https://api.zarz.moe/v1/qbz"
_META_API_FALLBACK = "https://api.zarz.moe/v1/qbz2"

_STREAM_APIS: list[str] = [
    "https://qbz.afkarxyz.qzz.io/api/track/",
    "https://qobuz.spotbye.qzz.io/api/track/",
]

_QOBUZ_DL_ : list[str] = [
    "https://qobuz.kennyy.com.br/api/download-music?",
    "https://qobuz.squid.wtf/api/download-music?",
    "https://mono.scavengerfurs.net/api/download-music?",
]

_POST_APIS: list[str] = [
    "https://api.zarz.moe/v1/dl/qbz",
    "https://api.zarz.moe/v1/dl/qbz2",
]

_GDSTUDIO_APIS: list[str] = [
    "https://music.gdstudio.xyz/api.php",
    "https://music.gdstudio.org/api.php",
]

_WJHE_APIS: list[str] = [
    "https://music.wjhe.top/api/music/qobuz/url",
]

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
    
    version = track.get("version", "").strip()
    display_title = f"{track.get('title', '')} ({version})" if version else track.get("title", "")
    display_norm = _normalize_search_text(display_title)

    performer = track.get("performer", {}).get("name", "")
    album_artist = track.get("album", {}).get("artist", {}).get("name", "")
    artist_norm = _normalize_search_text(performer or album_artist)
    album_norm = _normalize_search_text(track.get("album", {}).get("title", ""))
    
    score = 0

    if query_norm == title_norm or query_norm == display_norm:
        score += 1200
    elif (title_norm and query_norm in title_norm) or (display_norm and query_norm in display_norm):
        score += 420

    if artist_norm and artist_norm in query_norm: score += 180
    if album_norm and album_norm in query_norm: score += 100
    if track.get("isrc", "").strip(): score += 15
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

def _scrape_credentials(session: httpx.Client) -> QobuzCredentials: 
    headers = {"User-Agent": _DEFAULT_UA}
    resp    = session.get(f"{_OPEN_URL}1", headers=headers, timeout=15)
    resp.raise_for_status()

    m = _BUNDLE_RE.search(resp.text)
    if not m:
        raise RuntimeError("Qobuz bundle URL not found in HTML")

    bundle_url = m.group(1)
    if bundle_url.startswith("/"):
        bundle_url = "https://open.qobuz.com" + bundle_url

    bundle = session.get(bundle_url, headers=headers, timeout=30)
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
    if quality in ("27", "HI_RES_LOSSLESS", "DEFAULT", ""):
        return "hi-res-max"
    if quality in ("7", "HI_RES"):
        return "hi-res"
    return "cd"

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
    _URL_KEYS = ("download_url", "url", "link")
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
        from email.utils import parsedate_to_datetime
        import datetime
        dt = parsedate_to_datetime(raw)
        secs = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, secs)
    except Exception:
        return None

_gdstudio_ts9_cache: dict[str, tuple[str, float]] = {}
_gdstudio_ts9_lock  = threading.Lock()

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

def _fetch_stream_url_once(
        client:        httpx.Client,
        api_base:      str,
        track_id:      int,
        quality:       str,
        timeout_s:     int = _API_TIMEOUT_S,
        local_api_url: str | None = None,
) -> str:
    api_cleaning = api_base.rstrip('/')
    
    if local_api_url:
        local_api_url = local_api_url.rstrip('/')
    is_local_api = (api_cleaning == local_api_url) and bool(local_api_url)
    
    is_zarz = "zarz.moe" in api_cleaning
    is_gdstudio = "gdstudio" in api_cleaning
    is_wjhe = "wjhe.top" in api_cleaning
    is_squid = "squid.wtf" in api_cleaning
    
    is_post = api_base in _POST_APIS or is_zarz or is_gdstudio
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
            time.sleep(delay)

        try:
            if is_local_api:
                local_q = _map_local_api_quality(quality)
                url = f"{api_cleaning}/download-url/{track_id}?quality={local_q}"
                resp = client.get(url, headers=headers, timeout=timeout_s)

            elif is_gdstudio:
                host = urlparse(api_base).netloc
                ts9 = _get_gdstudio_ts9(host)
                br = "999" if quality in ("27", "7") else "740" if quality in ("", "6") else "320"
                
                payload = {
                    "types": "url",
                    "id": str(track_id),
                    "source": "qobuz",
                    "br": br,
                    "s": _build_gdstudio_signature(host, str(track_id), ts9)
                }
                
                gdstudio_headers = {
                    "User-Agent": _DEFAULT_UA,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": f"https://{host}",
                    "Referer": f"https://{host}/",
                    "Accept": "application/json"
                }
                resp = client.post(api_base, data=payload, headers=gdstudio_headers, timeout=timeout_s)

            elif is_wjhe:
                q_map = {"27": 2000, "7": 2000, "6": 1000, "": 1000}
                wjhe_q = q_map.get(quality, 1000)
                wjhe_f = "flac" 
                url = f"{api_base}?ID={track_id}&quality={wjhe_q}&format={wjhe_f}"
                
                resp = client.get(url, headers=headers, timeout=timeout_s, follow_redirects=False) 
                
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if loc and loc.startswith("http"):
                        return loc

            elif is_squid:
                import struct
                import base64
                
                parsed = urlparse(api_base)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                
                squid_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                }
                
                current_ts = int(time.time() * 1000)
                chal_resp = client.get(
                    f"{origin}/api/altcha/challenge",
                    params={"ts": current_ts},
                    headers=squid_headers,
                    timeout=timeout_s
                )
                chal_resp.raise_for_status()
                
                challenge_json_str = chal_resp.text
                challenge_data = json.loads(challenge_json_str)
                params = challenge_data["parameters"]
                
                salt_hex   = params.get("salt", "")
                nonce_hex  = params["nonce"]
                key_prefix = params["keyPrefix"]
                algorithm  = params.get("algorithm", "SHA-256")
                cost       = params.get("cost", 1)
                key_length = params.get("keyLength", 32)

                if algorithm != "SHA-256":
                    raise ValueError(f"Algoritmo ALTCHA non supportato: {algorithm}")

                salt_bytes  = bytes.fromhex(salt_hex) if salt_hex else b""
                nonce_bytes = bytes.fromhex(nonce_hex)

                start_time = time.time()
                counter = 0

                while True:
                    password = nonce_bytes + struct.pack(">I", counter)
                    for i in range(cost):
                        data = (salt_bytes + password) if i == 0 else derived
                        derived = hashlib.sha256(data).digest()[:key_length]

                    hex_digest = derived.hex()
                    if hex_digest.startswith(key_prefix):
                        break
                    counter += 1

                elapsed = (time.time() - start_time) * 1000
                min_elapsed = 160.0 + random.uniform(0, 20)
                if elapsed < min_elapsed:
                    time.sleep((min_elapsed - elapsed) / 1000.0)
                    elapsed = min_elapsed

                solution = {
                    "counter":    counter,
                    "derivedKey": hex_digest,
                    "time":       round(elapsed, 1),
                }
                
                solution_json = json.dumps(solution, separators=(",", ":"))
                payload_json  = f'{{"challenge":{challenge_json_str},"solution":{solution_json}}}'
                payload = base64.b64encode(payload_json.encode()).decode()
                
                verify_resp = client.post(
                    f"{origin}/api/altcha/verify",
                    json={"payload": payload},
                    headers={
                        "Origin":  origin,
                        "Referer": f"{origin}/",
                        **squid_headers
                    },
                    timeout=timeout_s,
                )
                verify_resp.raise_for_status()
                
                url = _build_stream_url(api_base, track_id, quality)
                resp = client.get(
                    url,
                    headers={
                        "Origin":  origin,
                        "Referer": f"{origin}/",
                        **squid_headers
                    },
                    timeout=timeout_s
                )

            elif is_post:
                if is_zarz:
                    from ..core.http import zarz_rate_limiter
                    zarz_rate_limiter.wait_for_slot()

                payload = {
                    "quality": _map_musicdl_quality(quality),
                    "upload_to_r2": False,
                    "url": f"{_OPEN_URL}{track_id}"
                }
                post_headers = {"User-Agent": _ZARZ_USER_AGENT if is_zarz else _DEFAULT_UA}

                resp = client.post(
                    api_base,
                    json=payload,
                    headers=post_headers,
                    timeout=timeout_s,
                )
            else:
                url = _build_stream_url(api_base, track_id, quality)
                resp = client.get(url, headers=headers, timeout=timeout_s)

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp)
                wait = _backoff_delay(attempt + 1, retry_after)
                last_err = RuntimeError("rate limited (HTTP 429)")
                if attempt < max_retries:
                    time.sleep(wait)
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
            continue
        except RuntimeError:
            raise
        except Exception as exc:
            last_err = exc
            if not is_post:
                break
            break

    raise last_err

def _fetch_stream_url_parallel(
        client:        httpx.Client,
        apis:          list[str],
        track_id:      int,
        quality:       str,
        timeout_s:     int = _API_TIMEOUT_S,
        local_api_url: str | None = None,
) -> tuple[str, str]:
    if not apis:
        raise SpotiflacError(ErrorKind.UNAVAILABLE, "no stream APIs configured", "qobuz")

    start  = time.time()
    errors: list[str] = []

    pool = ThreadPoolExecutor(max_workers=min(len(apis), 4))
    try:
        futures: dict[Future, str] = {
            pool.submit(_fetch_stream_url_once, client, api, track_id, quality, timeout_s, local_api_url): api
            for api in apis
        }
        for fut in as_completed(futures, timeout=timeout_s + 2):
            api = futures[fut]
            try:
                stream_url = fut.result()
                logger.debug("[qobuz] parallel: got URL from %s in %.2fs", api, time.time() - start)
                pool.shutdown(wait=False, cancel_futures=True)
                record_success("qobuz", api)
                print_source_banner("qobuz", api, quality)
                return api, stream_url
            except Exception as exc:
                err_msg = str(exc)[:80]
                errors.append(f"{api}: {err_msg}")
                record_failure("qobuz", api)
                print_api_failure("qobuz", api, err_msg)
    except FuturesTimeoutError:
        errors.append("global timeout exceeded")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    logger.debug("[qobuz] All APIs failed details: %s", "; ".join(errors))
    raise SpotiflacError(
        ErrorKind.UNAVAILABLE,
        f"All {len(apis)} Qobuz stream APIs failed.",
        "qobuz",
    )

# ---------------------------------------------------------------------------
# QobuzProvider
# ---------------------------------------------------------------------------
class QobuzProvider(BaseProvider):
    name = "qobuz"

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
        self._session     = NetworkManager.get_sync_client()
        self._creds:      QobuzCredentials | None = None
        self._creds_lock = threading.Lock()
        self._qobuz_token = qobuz_token or os.environ.get("QOBUZ_AUTH_TOKEN")
        self._local_api_url = local_api_url or os.environ.get("QOBUZ_LOCAL_API_URL")

    def _get_credentials(self, force_refresh: bool = False) -> QobuzCredentials:
        with self._creds_lock:
            if not force_refresh and self._creds and self._creds.is_fresh():
                if self._qobuz_token and not self._creds.user_auth_token:
                    self._creds.user_auth_token = self._qobuz_token
                return self._creds
            disk = _load_cached_credentials()
            if not force_refresh and disk and disk.is_fresh():
                self._creds = disk
                if self._qobuz_token and not self._creds.user_auth_token:
                    self._creds.user_auth_token = self._qobuz_token
                return self._creds

        scraped: QobuzCredentials | None = None
        try:
            candidate = _scrape_credentials(self._session)
            if self._probe_credentials(candidate):
                scraped = candidate
                _save_cached_credentials(scraped)
                logger.info("[qobuz] fresh credentials (app_id=%s)", scraped.app_id)
        except Exception as exc:
            logger.warning("[qobuz] credential refresh failed: %s", exc)

        with self._creds_lock:
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

    def _probe_credentials(self, creds: QobuzCredentials) -> bool:
        try:
            resp = self._do_signed_get("track/search", {"query": _PROBE_ISRC, "limit": "1"}, creds)
            return resp.json().get("tracks", {}).get("total", 0) > 0
        except Exception:
            return False

    def _do_signed_get(
            self,
            path:               str,
            params:             dict,
            creds:              QobuzCredentials | None = None,
            force_refresh:      bool = False,
            use_fallback_token: bool = False,
            _depth:             int  = 0,
    ) -> httpx.Response: 
        if creds is None:
            creds = self._get_credentials(force_refresh=force_refresh)

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

        resp = self._session.get(url, params=req_params, headers=headers, timeout=20)

        if resp.status_code in (400, 401) and _depth < 2:
            if creds.user_auth_token and not use_fallback_token and not force_refresh:
                return self._do_signed_get(
                    path, params, creds=creds,
                    force_refresh=False, use_fallback_token=True, _depth=_depth + 1,
                )
            if not force_refresh:
                return self._do_signed_get(
                    path, params,
                    force_refresh=True, use_fallback_token=use_fallback_token,
                    _depth=_depth + 1,
                )
        return resp

    def _search_by_isrc(self, isrc: str) -> dict:
        if isrc.startswith("qobuz_"):
            track_id = isrc.removeprefix("qobuz_")
            resp     = self._do_signed_get("track/get", {"track_id": track_id})
            if resp.status_code != 200:
                self._raise_api_error(resp, "track/get")
            return resp.json()

        resp = self._do_signed_get("track/search", {"query": isrc, "limit": "1"})
        if resp.status_code != 200:
            self._raise_api_error(resp, "track/search")

        body = resp.text
        if not body.strip():
            raise ParseError(self.name, "empty response from track/search")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ParseError(self.name, f"invalid JSON: {body[:200]}", exc)

        items = data.get("tracks", {}).get("items", [])
        if not items:
            raise TrackNotFoundError(self.name, isrc)
        return items[0]

    def _search_by_text(self, title: str, artist: str) -> dict | None:
        query = f"{title} {artist}".strip()
        try:
            resp = self._do_signed_get("track/search", {"query": query, "limit": "10"})
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

            # Ported Fallback Logic: Album Search Fallback (like searchTracksViaAlbumSearch in index.js)
            if not best_match or best_score < 400:
                album_resp = self._do_signed_get("album/search", {"query": query, "limit": "5"})
                if album_resp.status_code == 200:
                    albums = album_resp.json().get("albums", {}).get("items", [])
                    for album in albums:
                        album_id = album.get("id")
                        if not album_id: continue
                        # Fetch full album to score its tracks
                        track_resp = self._do_signed_get("album/get", {"album_id": album_id})
                        if track_resp.status_code == 200:
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
            logger.debug("[qobuz] Text search failed: %s", exc)
            return None

    def _get_stream_url(self, track_id: int, quality: str, allow_fallback: bool, exclude_apis: set[str] = None) -> tuple[str, str, str]:
        if exclude_apis is None:
            exclude_apis = set()
            
        chain = _QUALITY_FALLBACK.get(quality, [quality])
        
        all_apis = list(_STREAM_APIS) + list(_POST_APIS) + list(_GDSTUDIO_APIS) + list(_WJHE_APIS)
        ordered_apis = prioritize_providers("qobuz", all_apis)

        if self._local_api_url:
            cleaned_local_api = self._local_api_url.rstrip('/')
            if cleaned_local_api in ordered_apis:
                ordered_apis.remove(cleaned_local_api)
            ordered_apis.insert(0, cleaned_local_api)

        ordered_apis = [api for api in ordered_apis if api not in exclude_apis]

        if not allow_fallback:
            chain = chain[:1]

        local_api_url = self._local_api_url.rstrip('/') if self._local_api_url else None
        last_exc: Exception | None = None

        for i, q in enumerate(chain):
            if not ordered_apis:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, "All available endpoints have been excluded", self.name)
            
            try:
                winner_api, stream_url = _fetch_stream_url_parallel(
                    self._session, ordered_apis, track_id, q, _API_TIMEOUT_S, local_api_url
                )
                return winner_api, stream_url, q
            except SpotiflacError as exc:
                last_exc = exc
                if allow_fallback and i + 1 < len(chain):
                    print_quality_fallback("qobuz", q, chain[i + 1])
                    logger.warning("[qobuz] quality %s unavailable, trying %s", q, chain[i + 1])

        raise last_exc or SpotiflacError(
            ErrorKind.UNAVAILABLE,
            f"all quality levels exhausted for track {track_id}",
            self.name,
        )

    def _get_audio_duration_seconds(self, file_path: str) -> int:
        try:
            cmd = [
                "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", file_path
            ]
            return int(float(subprocess.check_output(cmd, text=True).strip()))
        except Exception:
            return 0

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
            quality:             str  = "6",
            embed_genre:         bool = True,
            single_genre:        bool = True,
            embed_lyrics:        bool = False,
            lyrics_providers:    list[str] | None = None,
            enrich_metadata:     bool = False,
            enrich_providers:    list[str] | None = None,
            is_album:            bool = False,
            **kwargs,
    ) -> DownloadResult:

        quality = _TIDAL_TO_QOBUZ_QUALITY.get(quality, quality)

        try:
            mb_fetcher = None
            if (enrich_metadata or embed_genre) and metadata.isrc:
                mb_fetcher = AsyncMBFetch(metadata.isrc)

            track = None
            if metadata.isrc:
                try:
                    track = self._search_by_isrc(metadata.isrc)
                except Exception as e:
                    logger.debug("[qobuz] ISRC %s not found, trying textual fallback. Error: %s", metadata.isrc, e)

            if not track:
                logger.info("[qobuz] Trying textual search for: %s - %s", metadata.title, metadata.artists)
                track = self._search_by_text(metadata.title, metadata.artists)

            if not track:
                raise TrackNotFoundError(self.name, f"Track not found (ISRC: {metadata.isrc}, Title: {metadata.title})")

            track_id = track.get("id")
            if not track_id:
                raise TrackNotFoundError(self.name, "Missing track ID in Qobuz response")
                
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
            
            # --- DATI BASE E GERARCHICI ---
            if album_data.get("genre") and album_data["genre"].get("name"):
                qobuz_extra_tags["GENRE"] = album_data["genre"]["name"]
                
            if album_data.get("label") and album_data["label"].get("name"):
                qobuz_extra_tags["LABEL"] = album_data["label"]["name"]
                qobuz_extra_tags["ORGANIZATION"] = album_data["label"]["name"]
                
            if album_data.get("upc"):
                qobuz_extra_tags["BARCODE"] = album_data["upc"]
                qobuz_extra_tags["UPC"] = album_data["upc"]

            # --- DETTAGLI AUDIO E TECNICI ---
            if album_data.get("maximum_technical_specifications"):
                qobuz_extra_tags["TECHNICAL_SPECIFICATIONS"] = album_data["maximum_technical_specifications"]
            
            # ReplayGain
            audio_info = track.get("audio_info", {})
            if "replaygain_track_gain" in audio_info:
                qobuz_extra_tags["REPLAYGAIN_TRACK_GAIN"] = f"{audio_info['replaygain_track_gain']} dB"
            if "replaygain_track_peak" in audio_info:
                qobuz_extra_tags["REPLAYGAIN_TRACK_PEAK"] = str(audio_info['replaygain_track_peak'])

            # --- CREDITI E TESTI ---
            if track.get("performers"):
                qobuz_extra_tags["COMMENT"] = track["performers"]
            if track.get("parental_warning"):
                qobuz_extra_tags["ITUNESADVISORY"] = "1"

            # --- ID E URL ---
            qobuz_track_id = str(track.get("id", ""))
            qobuz_album_id = str(album_data.get("qobuz_id", ""))
            if qobuz_track_id: qobuz_extra_tags["QOBUZ_TRACK_ID"] = qobuz_track_id
            if qobuz_album_id: qobuz_extra_tags["QOBUZ_ALBUM_ID"] = qobuz_album_id
            if album_data.get("url"): qobuz_extra_tags["URL"] = album_data["url"]

            # --- AWARDS ---
            awards = album_data.get("awards", [])
            if awards:
                qobuz_extra_tags["AWARDS"] = ", ".join([a.get("name") for a in awards])
                
            if track.get("isrc"):
                metadata.isrc = track["isrc"]
                
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
            res: dict = {}
            if mb_fetcher:
                res = mb_fetcher.future.result()

            mb_tags = mb_result_to_tags(res)
            mb_tags.update(qobuz_extra_tags)
            _print_mb_summary(mb_tags)
            
            while not valid:
                try:
                    winner_api, stream_url, used_quality = self._get_stream_url(
                        track_id, quality, allow_fallback, exclude_apis=excluded_apis
                    )
                except SpotiflacError as exc:
                    if last_err:
                        raise SpotiflacError(
                            ErrorKind.UNAVAILABLE, 
                            f"All APIs failed (errors or previews). Last reason: {last_err}", 
                            self.name
                        )
                    raise exc

                self._http.stream_to_file(stream_url, str(dest), self._progress_cb)

                valid, err = validate_downloaded_track(str(dest), expected_s)
                if not valid:
                    actual_duration = self._get_audio_duration_seconds(str(dest))
                    if actual_duration > 0 and actual_duration <= 35 and expected_s > 45:
                        err = "Preview-length audio detected (30s limit)"
                        
                    logger.warning("[qobuz] API %s returned invalid file: %s. Blacklisting endpoint and retrying...", winner_api, err)
                    record_failure("qobuz", winner_api)  
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
                    embed_metadata(str(dest), metadata, opts, session=self._session)
                except SpotiflacError as exc:
                    message = str(exc).lower()
                    if exc.kind == ErrorKind.FILE_IO and "not a valid flac file" in message:
                        logger.warning(
                            "[qobuz] API %s returned invalid FLAC file: %s. Blacklisting endpoint and retrying...",
                            winner_api, exc,
                        )
                        record_failure("qobuz", winner_api)
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

    @staticmethod
    def _raise_api_error(resp: httpx.Response, endpoint: str) -> None: 
        try:
            msg = resp.json().get("message", f"HTTP {resp.status_code}")
        except Exception:
            msg = f"HTTP {resp.status_code}"
        raise NetworkError("qobuz", f"{endpoint} → {msg}")
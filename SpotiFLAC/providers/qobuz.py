from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from ..core.tagger import _print_mb_summary
from dataclasses import dataclass, field
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import requests

from ..core.errors import (
    AuthError, TrackNotFoundError, NetworkError,
    ParseError, SpotiflacError, ErrorKind,
)
from ..core.http import HttpClient, RetryConfig
from ..core.models import TrackMetadata, DownloadResult
from ..core.tagger import embed_metadata
from ..core.provider_stats import record_success, record_failure, prioritize_providers
from .base import BaseProvider
from ..core.musicbrainz import AsyncMBFetch
from ..core.download_validation import validate_downloaded_track
from ..core.console import (
    print_source_banner, print_api_failure, print_quality_fallback,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_BASE       = "https://www.qobuz.com/api.json/0.2"
_DEFAULT_APP_ID = "712109809"
_DEFAULT_APP_SECRET = "589be88e4538daea11f509d29e4a23b1"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
_CREDS_TTL        = 24 * 3600
_PROBE_ISRC       = "USUM71703861"
_OPEN_URL         = "https://open.qobuz.com/track/"
_CREDS_CACHE_FILE = os.path.join(
    os.path.expanduser("~"), ".cache", "spotiflac", "qobuz-credentials.json"
)

_BUNDLE_RE    = re.compile(
    r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"'
)
_API_CONFIG_RE = re.compile(
    r'app_id:"(?P<app_id>\d{9})",app_secret:"(?P<app_secret>[a-f0-9]{32})"'
)

_STREAM_APIS: list[str] = [
    "https://dab.yeet.su/api/stream?trackId=",
    "https://dabmusic.xyz/api/stream?trackId=",
    "https://qbz.afkarxyz.qzz.io/api/track/",
    "https://qobuz.spotbye.qzz.io/api/track/",
    "https://qobuz.squid.wtf/api/download-music?country=US&track_id=",
    "https://dl.musicdl.me/qobuz/download",
    "https://api.zarz.moe/dl/qbz",
    "https://www.musicdl.me/api/qobuz/download"
]

_MUSICDL_APIS = {
    "https://www.musicdl.me/api/qobuz/download",
    "https://dl.musicdl.me/qobuz/download",
    "https://api.zarz.moe/dl/qbz",
}

_QUALITY_FALLBACK: dict[str, list[str]] = {
    "27": ["27", "7", "6"],
    "7":  ["7", "6"],
    "6":  ["6"],
    "5":  ["6"],
    "":   ["6"],
}

# FIX #3: mappa i valori Tidal-style verso i codici numerici Qobuz
_TIDAL_TO_QOBUZ_QUALITY: dict[str, str] = {
    "HI_RES":  "27",
    "LOSSLESS": "6",
}

_API_TIMEOUT_S = 8
_MAX_RETRIES   = 1
_RETRY_DELAY_S = 0.3


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
        return (
                bool(self.app_id)
                and bool(self.app_secret)
                and (time.time() - self.fetched_at) < _CREDS_TTL
        )

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
        return cls(_DEFAULT_APP_ID, _DEFAULT_APP_SECRET, "embedded-default")

_QOBUZ_MUSICDL_SEED = bytes([
    0x73,0x70,0x6f,0x74,0x69,0x66,
    0x6c,0x61,0x63,0x3a,0x71,0x6f,
    0x62,0x75,0x7a,0x3a,0x6d,0x75,0x73,0x69,0x63,0x64,0x6c,0x3a,0x76,0x31,
])
_QOBUZ_MUSICDL_AAD = bytes([
    0x71,0x6f,0x62,0x75,0x7a,0x7c,0x6d,0x75,0x73,0x69,0x63,0x64,
    0x6c,0x7c,0x64,0x65,0x62,0x75,0x67,0x7c,0x76,0x31,
])
_QOBUZ_MUSICDL_NONCE = bytes([
    0x91,0x2a,0x5c,0x77,0x0f,0x33,0xa8,0x14,0x62,0x9d,0xce,0x41,
])
_QOBUZ_MUSICDL_CIPHERTEXT_TAG = bytes([
    0xf3,0x4a,0x83,0x45,0x24,0xb6,0x22,0xaf,0xd6,0xc3,0x6e,0x2d,
    0x56,0xd1,0xbb,0x0b,0xe9,0x1b,0x4f,0x1c,0x5f,0x41,0x55,0xc2,
    0xc6,0xdf,0xad,0x21,0x58,0xfe,0xd5,0xb8,0x2d,0x29,0xf9,0x9e,
    0x6f,0xd6,
    0x69,0x0c,0x42,0x70,0x14,0x83,0xff,0x14,0xc8,0xbe,0x17,0x00,
    0x69,0xb1,0xfe,0xbb,
])

_qobuz_musicdl_key: str | None = None

def _get_qobuz_musicdl_key() -> str:
    global _qobuz_musicdl_key
    if _qobuz_musicdl_key is not None:
        return _qobuz_musicdl_key
    key = hashlib.sha256(_QOBUZ_MUSICDL_SEED).digest()
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(
        _QOBUZ_MUSICDL_NONCE,
        _QOBUZ_MUSICDL_CIPHERTEXT_TAG,
        _QOBUZ_MUSICDL_AAD,
    )
    _qobuz_musicdl_key = plaintext.decode()
    return _qobuz_musicdl_key


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


def _scrape_credentials(session: requests.Session) -> QobuzCredentials:
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
    if api_base.endswith("="):
        return f"{api_base}{track_id}&quality={quality}"
    return f"{api_base}{track_id}?quality={quality}"


def _map_musicdl_quality(quality: str) -> str:
    if quality == "27":
        return "hi-res-max"
    if quality == "7":
        return "hi-res"
    return "cd"


# ---------------------------------------------------------------------------
# Fetch logica per API miste (GET / POST)
# ---------------------------------------------------------------------------
def _extract_stream_url_from_json(data: dict) -> str | None:
    for key in ("download_url", "url", "link"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("download_url", "url", "link"):
            val = nested.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    return None


def _fetch_stream_url_once(
        api_base:  str,
        track_id:  int,
        quality:   str,
        timeout_s: int = _API_TIMEOUT_S,
) -> str:
    is_musicdl = api_base in _MUSICDL_APIS
    delay      = _RETRY_DELAY_S
    last_err: Exception = RuntimeError("no attempts made")

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            logger.debug("[qobuz] retry %d/%d for %s after %.1fs", attempt, _MAX_RETRIES, api_base, delay)
            time.sleep(delay)
            delay *= 2

        try:
            if is_musicdl:
                payload = {
                    "quality": _map_musicdl_quality(quality),
                    "upload_to_r2": False,
                    "url": f"{_OPEN_URL}{track_id}"
                }
                resp = requests.post(
                    api_base,
                    json=payload,
                    headers={"User-Agent": _DEFAULT_UA},
                    timeout=timeout_s,
                )
            else:
                url = _build_stream_url(api_base, track_id, quality)
                resp = requests.get(
                    url,
                    headers={"User-Agent":  _DEFAULT_UA},
                    timeout=timeout_s,
                )

            if resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                continue
            if resp.status_code == 429:
                delay = max(delay, 2.0)
                last_err = RuntimeError("rate limited")
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

            if not isinstance(data, dict):
                last_err = RuntimeError("unexpected JSON type")
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

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = exc
            continue
        except RuntimeError:
            raise
        except Exception as exc:
            last_err = exc
            break

    raise last_err


def _fetch_stream_url_parallel(
        apis:      list[str],
        track_id:  int,
        quality:   str,
        timeout_s: int = _API_TIMEOUT_S,
) -> tuple[str, str]:
    if not apis:
        raise SpotiflacError(ErrorKind.UNAVAILABLE, "no stream APIs configured", "qobuz")

    start  = time.time()
    errors: list[str] = []

    pool = ThreadPoolExecutor(max_workers=min(len(apis), 8))
    try:
        futures: dict[Future, str] = {
            pool.submit(_fetch_stream_url_once, api, track_id, quality, timeout_s): api
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
    except (TimeoutError, FuturesTimeoutError):
        errors.append("global timeout exceeded")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    raise SpotiflacError(
        ErrorKind.UNAVAILABLE,
        f"all {len(apis)} Qobuz stream APIs failed in {time.time()-start:.1f}s — {'; '.join(errors)}",
        "qobuz",
    )


# ---------------------------------------------------------------------------
# QobuzProvider
# ---------------------------------------------------------------------------
class QobuzProvider(BaseProvider):
    name = "qobuz"

    def __init__(self, timeout_s: int = 30, qobuz_token: str | None = None) -> None:
        super().__init__(
            timeout_s = timeout_s,
            retry     = RetryConfig(max_attempts=2),
            headers   = {"User-Agent": _DEFAULT_UA, "Accept": "application/json"},
        )
        self._session    = self._http._session
        self._creds:      QobuzCredentials | None = None
        self._creds_lock = threading.Lock()
        self._qobuz_token = qobuz_token or os.environ.get("QOBUZ_AUTH_TOKEN")

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
    ) -> requests.Response:
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

    def _get_stream_url(self, track_id: int, quality: str, allow_fallback: bool) -> str:
        chain = _QUALITY_FALLBACK.get(quality, [quality])
        ordered_apis = prioritize_providers("qobuz", list(_STREAM_APIS))
        if not allow_fallback:
            chain = chain[:1]

        last_exc: Exception | None = None

        for i, q in enumerate(chain):
            try:
                winner_api, stream_url = _fetch_stream_url_parallel(ordered_apis, track_id, q, _API_TIMEOUT_S)
                return stream_url
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
            embed_lyrics:            bool = False,
            lyrics_providers:        list[str] | None = None,
            lyrics_spotify_token:    str = "",
            enrich_metadata:         bool = False,
            enrich_providers:        list[str] | None = None,
    ) -> DownloadResult:
        # FIX #3: normalizza la quality Tidal-style verso i codici numerici Qobuz.
        # Se quality è "LOSSLESS" o "HI_RES" (passata da downloader.py dopo il Fix #1),
        # la convertiamo nel formato atteso da Qobuz prima di qualsiasi uso.
        quality = _TIDAL_TO_QOBUZ_QUALITY.get(quality, quality)

        try:
            if not metadata.isrc:
                raise TrackNotFoundError(self.name, "no ISRC provided")

            mb_fetcher = None
            if (enrich_metadata or embed_genre) and metadata.isrc:
                mb_fetcher = AsyncMBFetch(metadata.isrc)

            track    = self._search_by_isrc(metadata.isrc)
            track_id = track.get("id")
            if not track_id:
                raise TrackNotFoundError(self.name, metadata.isrc)

            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            stream_url = self._get_stream_url(track_id, quality, allow_fallback)
            self._http.stream_to_file(stream_url, str(dest), self._progress_cb)

            expected_s = metadata.duration_ms // 1000
            valid, err = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, err, self.name)

            mb_tags = {}
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
            _print_mb_summary(mb_tags)

            embed_metadata(
                dest, metadata,
                first_artist_only       = first_artist_only,
                cover_url               = metadata.cover_url,
                session                 = self._session,
                extra_tags              = mb_tags,
                embed_lyrics            = embed_lyrics,
                lyrics_providers        = lyrics_providers,
                lyrics_spotify_token    = lyrics_spotify_token,
                enrich                  = enrich_metadata,
                enrich_providers        = enrich_providers,
                enrich_qobuz_token      = self._qobuz_token or "",
            )
            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[qobuz] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[qobuz] unexpected error")
            return DownloadResult.fail(self.name, f"unexpected: {exc}")

    @staticmethod
    def _raise_api_error(resp: requests.Response, endpoint: str) -> None:
        try:
            msg = resp.json().get("message", f"HTTP {resp.status_code}")
        except Exception:
            msg = f"HTTP {resp.status_code}"
        raise NetworkError("qobuz", f"{endpoint} → {msg}")
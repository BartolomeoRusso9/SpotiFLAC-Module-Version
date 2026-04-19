"""
QobuzProvider — allineato all'implementazione Go di riferimento.

Differenze rispetto alla versione precedente:
- _get_stream_url usa prioritize() invece di random.shuffle
  (le API che funzionano vengono promosse, quelle rotte penalizzate)
- record_success/record_failure chiamati dopo ogni tentativo
- _signed_get usa doQobuzSignedRequest pattern: retry con force_refresh inline
- Credenziali per-istanza con lock proprio
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_BASE           = "https://www.qobuz.com/api.json/0.2"
_DEFAULT_APP_ID     = "712109809"
_DEFAULT_APP_SECRET = "589be88e4538daea11f509d29e4a23b1"
_DEFAULT_UA         = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
_CREDS_TTL        = 24 * 3600
_PROBE_ISRC       = "USUM71703861"
_OPEN_URL         = "https://open.qobuz.com/track/1"
_CREDS_CACHE_FILE = os.path.join(
    os.path.expanduser("~"), ".cache", "spotiflac", "qobuz-credentials.json"
)

_BUNDLE_RE     = re.compile(
    r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"'
)
_API_CONFIG_RE = re.compile(
    r'app_id:"(?P<app_id>\d{9})",app_secret:"(?P<app_secret>[a-f0-9]{32})"'
)

# Stream API pool — ordinato da prioritize() a runtime
_STREAM_APIS = [
    "https://dab.yeet.su/api/stream?trackId=",
    "https://dabmusic.xyz/api/stream?trackId=",
    "https://qbz.afkarxyz.qzz.io/api/track/",
]

# Qualità fallback chain (identica al Go)
_QUALITY_FALLBACK: dict[str, list[str]] = {
    "27": ["27", "7", "6"],
    "7":  ["7", "6"],
    "6":  ["6"],
    "5":  ["6"],
    "":   ["6"],
}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass
class QobuzCredentials:
    app_id:     str
    app_secret: str
    source:     str   = "embedded-default"
    fetched_at: float = field(default_factory=time.time)
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
            app_id     = d.get("app_id", ""),
            app_secret = d.get("app_secret", ""),
            source     = d.get("source", ""),
            fetched_at = float(d.get("fetched_at_unix", 0)),
            user_auth_token = token,
        )

    @classmethod
    def default(cls) -> "QobuzCredentials":
        return cls(_DEFAULT_APP_ID, _DEFAULT_APP_SECRET, "embedded-default")


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
    resp    = session.get(_OPEN_URL, headers=headers, timeout=15)
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
# Signature helpers (pure functions — identiche al Go)
# ---------------------------------------------------------------------------

def _compute_signature(path: str, params: dict, timestamp: str, secret: str) -> str:
    """
    Replica esatta di qobuzRequestSignature() del Go:
    - normalizza il path (strip slash)
    - esclude app_id, request_ts, request_sig
    - ordina le chiavi rimanenti alfabeticamente
    - concatena: normalizedPath + chiave+valore + timestamp + secret
    - MD5 hex
    """
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
    """Equivalente a buildQobuzAPIURL() del Go."""
    if "qbz.afkarxyz" in api_base:
        return f"{api_base}{track_id}?quality={quality}"
    return f"{api_base}{track_id}&quality={quality}"


# ---------------------------------------------------------------------------
# QobuzProvider
# ---------------------------------------------------------------------------

class QobuzProvider(BaseProvider):
    name = "qobuz"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(
            timeout_s = timeout_s,
            retry     = RetryConfig(max_attempts=2),
            headers   = {"User-Agent": _DEFAULT_UA, "Accept": "application/json"},
        )
        self._session    = self._http._session
        self._creds:      QobuzCredentials | None = None
        self._creds_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Credential management — equivalente a getQobuzAPICredentials() Go
    # ------------------------------------------------------------------

    def _get_credentials(self, force_refresh: bool = False) -> QobuzCredentials:
        with self._creds_lock:
            if not force_refresh and self._creds and self._creds.is_fresh():
                return self._creds

            disk = _load_cached_credentials()
            if not force_refresh and disk and disk.is_fresh():
                self._creds = disk
                return self._creds

            try:
                scraped = _scrape_credentials(self._session)
                if self._probe_credentials(scraped):
                    self._creds = scraped
                    _save_cached_credentials(scraped)
                    logger.info("[qobuz] Fresh credentials from %s (app_id=%s)",
                                scraped.source, scraped.app_id)
                    return self._creds
                raise RuntimeError("scraped credentials failed validation probe")
            except Exception as exc:
                logger.warning("[qobuz] Failed to refresh credentials: %s", exc)

            if disk:
                logger.warning("[qobuz] Using stale cached credentials")
                self._creds = disk
                return self._creds

            if self._creds:
                return self._creds

            logger.warning("[qobuz] Using embedded fallback credentials")
            self._creds = QobuzCredentials.default()
            return self._creds

    def _probe_credentials(self, creds: QobuzCredentials) -> bool:
        """Equivalente a qobuzCredentialsSupportSignedMetadata() del Go."""
        try:
            resp = self._do_signed_get(
                "track/search", {"query": _PROBE_ISRC, "limit": "1"}, creds
            )
            return resp.json().get("tracks", {}).get("total", 0) > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Signed request — equivalente a doQobuzSignedRequest() del Go
    # Con refresh automatico su 400/401 (qobuzShouldRefreshCredentials)
    # ------------------------------------------------------------------

    def _do_signed_get(
        self,
        path:          str,
        params:        dict,
        creds:         QobuzCredentials | None = None,
        force_refresh: bool = False,
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
        url  = f"{_API_BASE}/{path.strip('/')}"

        headers = {"X-App-Id": creds.app_id}
        if creds.user_auth_token:
            headers["X-User-Auth-Token"] = creds.user_auth_token

        resp = self._session.get(
            url, params=req_params,
            headers=headers,
            timeout=20,
        )

        # qobuzShouldRefreshCredentials: 400 o 401 → refresh e riprova
        if resp.status_code in (400, 401) and not force_refresh:
            logger.info("[qobuz] HTTP %s — forcing credential refresh", resp.status_code)
            return self._do_signed_get(path, params, force_refresh=True)

        return resp

    # ------------------------------------------------------------------
    # Track lookup
    # ------------------------------------------------------------------

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
            raise ParseError(self.name, "Empty response from track/search")

        try:
            data = resp.json()
        except ValueError as exc:
            preview = body[:200] + ("..." if len(body) > 200 else "")
            raise ParseError(self.name, f"Invalid JSON: {preview}", exc)

        items = data.get("tracks", {}).get("items", [])
        if not items:
            raise TrackNotFoundError(self.name, isrc)
        return items[0]

    # ------------------------------------------------------------------
    # Stream URL — con prioritize() invece di shuffle casuale
    # Equivalente a GetDownloadURL() del Go
    # ------------------------------------------------------------------

    def _get_official_stream(self, track_id: int, quality: str) -> str:
        """Ottiene l'URL dello stream usando l'API ufficiale e il token utente."""
        resp = self._do_signed_get(
            "track/getFileUrl",
            {
                "track_id": str(track_id),
                "format_id": quality,
                "intent": "stream"
            }
        )
        if resp.status_code != 200:
            self._raise_api_error(resp, "track/getFileUrl")

        data = resp.json()
        if "url" not in data:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"No stream URL returned by official API for track {track_id}",
                self.name
            )
        return data["url"]

    def _try_quality(self, track_id: int, quality: str) -> str:
        """
        Prova tutte le API nell'ordine dato da prioritize().
        Registra successi/fallimenti per il sistema di scoring.
        """
        ordered_apis = prioritize_providers("qobuz", _STREAM_APIS)

        last_err = ""
        for api in ordered_apis:
            url = _build_stream_url(api, track_id, quality)
            try:
                resp = self._session.get(
                    url,
                    headers={"User-Agent": _DEFAULT_UA},
                    timeout=20,
                )
                if resp.status_code != 200 or not resp.text.strip():
                    last_err = f"HTTP {resp.status_code} or empty body"
                    record_failure("qobuz", api)
                    continue

                data = resp.json()
                if isinstance(data, dict):
                    stream = data.get("url") or data.get("data", {}).get("url")
                    if stream:
                        record_success("qobuz", api)
                        logger.debug("[qobuz] Stream URL via %s (quality=%s)", api, quality)
                        return stream

                last_err = "no URL in response"
                record_failure("qobuz", api)

            except Exception as exc:
                last_err = str(exc)
                record_failure("qobuz", api)
                logger.debug("[qobuz] API %s failed: %s", api, exc)

        raise SpotiflacError(
            ErrorKind.UNAVAILABLE,
            f"All stream APIs failed for track {track_id} quality={quality} (last: {last_err})",
            self.name,
        )

    def _get_stream_url(self, track_id: int, quality: str, allow_fallback: bool) -> str:
        """
        Equivalente a GetDownloadURL() del Go:
        prova la qualità richiesta, poi scala in basso se allow_fallback=True.
        """
        chain = _QUALITY_FALLBACK.get(quality, [quality])
        if not allow_fallback:
            chain = [chain[0]]

        creds = self._get_credentials()
        has_token = bool(creds.user_auth_token)

        last_exc: SpotiflacError | None = None
        for q in chain:
            try:
                logger.debug("[qobuz] Trying public APIs for track %s (quality=%s)", track_id, q)
                return self._try_quality(track_id, q)
            except Exception as public_exc:
                logger.debug("[qobuz] Public APIs failed for quality %s: %s", q, public_exc)
                if has_token:
                    try:
                        logger.info("[qobuz] Public APIs failed, falling back to OFFICIAL API for quality %s", q)
                        return self._get_official_stream(track_id, q)
                    except Exception as official_exc:
                        logger.warning("[qobuz] Official API also failed for quality %s: %s", q, official_exc)
                        if isinstance(official_exc, SpotiflacError):
                            last_exc = official_exc
                        else:
                            last_exc = SpotiflacError(ErrorKind.UNAVAILABLE, str(official_exc), self.name)
                else:
                    if isinstance(public_exc, SpotiflacError):
                        last_exc = public_exc
                    else:
                        last_exc = SpotiflacError(ErrorKind.UNAVAILABLE, str(public_exc), self.name)

                logger.warning("[qobuz] Quality %s completely unavailable, trying next fallback", q)
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Public download interface
    # ------------------------------------------------------------------

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
    ) -> DownloadResult:
        try:
            if not metadata.isrc:
                raise TrackNotFoundError(self.name, "no ISRC provided")

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

            embed_metadata(
                dest, metadata,
                first_artist_only = first_artist_only,
                cover_url         = metadata.cover_url,
                session           = self._session,
            )

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[qobuz] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[qobuz] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _raise_api_error(resp: requests.Response, endpoint: str) -> None:
        try:
            msg = resp.json().get("message", f"HTTP {resp.status_code}")
        except Exception:
            msg = f"HTTP {resp.status_code}"
        raise NetworkError("qobuz", f"{endpoint} → {msg}")

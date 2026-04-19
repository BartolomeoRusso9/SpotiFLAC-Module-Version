"""
SpotiDownloaderProvider — refactored.

Cambiamenti rispetto al codice originale:
- Token cached come istanza, non classe-level (evita stato condiviso tra istanze)
- fetch_token con retry pulito tramite HttpClient
- Token refresh automatico su 401/403 (senza logica duplicata)
- embed_metadata delegato al tagger centralizzato
"""
from __future__ import annotations
import logging
import time
from pathlib import Path

import requests

from ..core.errors import AuthError, TrackNotFoundError, SpotiflacError, ErrorKind
from ..core.http import HttpClient, RetryConfig
from ..core.models import TrackMetadata, DownloadResult
from ..core.tagger import embed_metadata, max_resolution_spotify_cover
from .base import BaseProvider

logger = logging.getLogger(__name__)

_TOKEN_URL     = "https://spdl.afkarxyz.qzz.io/token"
_DOWNLOAD_URL  = "https://api.spotidownloader.com/download"
_ORIGIN        = "https://spotidownloader.com"
_UA            = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class SpotiDownloaderProvider(BaseProvider):
    name = "spoti"

    def __init__(self, timeout_s: int = 15) -> None:
        super().__init__(
            timeout_s = timeout_s,
            retry     = RetryConfig(max_attempts=3, base_delay_s=1.0),
            headers   = {"User-Agent": _UA},
        )
        self._session = self._http._session
        self._token:  str   = ""
        self._token_ts: float = 0.0

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_token(self, force_refresh: bool = False) -> str:
        """Ritorna un token valido, scaricandone uno nuovo se necessario."""
        if not force_refresh and self._token:
            return self._token

        logger.info("[spoti] Fetching session token…")
        for attempt in range(1, 4):
            try:
                resp = self._session.get(_TOKEN_URL, timeout=15)
                resp.raise_for_status()
                token = resp.json().get("token")
                if token:
                    self._token    = token
                    self._token_ts = time.time()
                    return self._token
            except Exception as exc:
                if attempt == 3:
                    raise AuthError(self.name, f"Failed to fetch token after 3 attempts: {exc}", exc)
                time.sleep(1)

        raise AuthError(self.name, "Token not found in API response")

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Origin":        _ORIGIN,
            "Referer":       f"{_ORIGIN}/",
        }

    # ------------------------------------------------------------------
    # Stream URL
    # ------------------------------------------------------------------

    def _get_flac_url(self, track_id: str, token: str) -> str:
        headers = {
            **self._auth_headers(token),
            "Content-Type": "application/json",
        }
        resp = self._session.post(
            _DOWNLOAD_URL,
            json    = {"id": track_id, "flac": True},
            headers = headers,
            timeout = 15,
        )

        # Token expired — refresh once and retry
        if resp.status_code in (401, 403):
            logger.info("[spoti] Token expired — refreshing")
            token    = self._get_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            resp = self._session.post(
                _DOWNLOAD_URL,
                json    = {"id": track_id, "flac": True},
                headers = headers,
                timeout = 15,
            )

        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            raise SpotiflacError(ErrorKind.UNAVAILABLE, "API returned success=false", self.name)

        def is_flac(link: str) -> bool:
            return bool(link) and link.split("?")[0].lower().endswith(".flac")

        for key in ("linkFlac", "link"):
            link = data.get(key, "")
            if is_flac(link):
                return link

        raise SpotiflacError(
            ErrorKind.UNAVAILABLE, "No FLAC link in response (MP3 only?)", self.name
        )

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
    ) -> DownloadResult:
        try:
            track_id = metadata.id.split("/")[-1].split("?")[0]

            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            token    = self._get_token()
            flac_url = self._get_flac_url(track_id, token)

            cover_url = max_resolution_spotify_cover(metadata.cover_url)

            self._http.stream_to_file(
                flac_url,
                str(dest),
                progress_cb    = self._progress_cb,
                extra_headers  = self._auth_headers(token),
            )

            embed_metadata(
                dest, metadata,
                first_artist_only = first_artist_only,
                cover_url         = cover_url,
                session           = self._session,
            )

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[spoti] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[spoti] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

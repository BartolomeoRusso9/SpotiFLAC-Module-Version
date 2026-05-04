from __future__ import annotations

import os
import logging
import threading
import time

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError, AuthError, TrackNotFoundError, ErrorKind
from ..core.tagger import embed_metadata, _print_mb_summary
from ..core.musicbrainz import AsyncMBFetch
from ..core.download_validation import validate_downloaded_track
from .base import BaseProvider

logger = logging.getLogger(__name__)

_API_BASE = "https://api.spotidownloader.com"
_ORIGIN   = "https://spotidownloader.com"
_SESSION_URL = f"{_API_BASE}/session"

class SpotiDownloaderProvider(BaseProvider):
    name = "spotidownloader"

    _token: str = ""
    _token_exp: float = 0.0
    _bootstrap_token: str = ""
    _lock = threading.Lock()

    def __init__(self, timeout_s: int = 30):
        super().__init__(timeout_s=timeout_s)

    # ---------------------------------------------------------
    # BOOTSTRAP TOKEN SCRAPER
    # ---------------------------------------------------------

    def _fetch_bootstrap_token(self) -> str:
        try:
            resp = self._http.post_json(
                _SESSION_URL,
                json={"token": self.__class__._bootstrap_token or "init"},
                headers={
                    "Origin": _ORIGIN,
                    "Referer": f"{_ORIGIN}/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                },
            )
        except Exception as exc:
            if "403" in str(exc):
                raise AuthError(
                    self.name,
                    "Accesso negato dal server (probabile blocco Cloudflare/WAF attivo). "
                    "Il provider potrebbe essere temporaneamente inaccessibile."
                ) from exc
            raise AuthError(self.name, f"session request failed: {exc}")

        if not resp or not resp.get("success"):
            raise AuthError(self.name, "bootstrap session failed")

        token = resp.get("token")
        if not token:
            raise AuthError(self.name, "no token in session response")

        return token

    # ---------------------------------------------------------
    # TOKEN CACHE
    # ---------------------------------------------------------

    def _get_token(self) -> str:
        now = time.monotonic()

        if self.__class__._token and now < self.__class__._token_exp:
            return self.__class__._token

        with self.__class__._lock:
            now = time.monotonic()

            if self.__class__._token and now < self.__class__._token_exp:
                return self.__class__._token

            token = self._fetch_bootstrap_token()

            self.__class__._bootstrap_token = token
            self.__class__._token = token
            self.__class__._token_exp = now + (55 * 60)

            logger.info("[%s] token aggiornato via /session", self.name)
            return token

    def invalidate_token(self):
        with self.__class__._lock:
            self.__class__._token = ""
            self.__class__._token_exp = 0.0

    # ---------------------------------------------------------
    # RESOLVE FLAC (NIENTE FALLBACK MP3)
    # ---------------------------------------------------------

    def _get_flac_url(self, spotify_id: str, token: str) -> str:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": _ORIGIN,
            "Referer": f"{_ORIGIN}/",
        }

        payload = {"id": spotify_id, "flac": True}

        try:
            data = self._http.post_json(
                f"{_API_BASE}/download",
                json=payload,
                headers=headers,
            )
        except Exception:
            self.invalidate_token()
            raise

        if not data or not data.get("success"):
            raise TrackNotFoundError(self.name, spotify_id)

        flac = data.get("linkFlac")
        normal = data.get("link")

        # Esige che il link contenga tassativamente l'estensione .flac
        for url in (flac, normal):
            if url and ".flac" in url:
                return url

        raise TrackNotFoundError(self.name, "FLAC non disponibile per questa traccia")

    # ---------------------------------------------------------
    # DOWNLOAD PIPELINE
    # ---------------------------------------------------------

    def download_track(
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
            lyrics_providers=None,
            lyrics_spotify_token: str = "",
            enrich_metadata: bool = False,
            enrich_providers=None,
            is_album:                bool            = False,
            **kwargs,
    ) -> DownloadResult:

        try:
            # 1. Avvia MusicBrainz in background per il fetching parallelo
            mb_fetcher = None
            if metadata.isrc:
                mb_fetcher = AsyncMBFetch(metadata.isrc)

            # 2. Ottieni Token e URL FLAC
            token = self._get_token()
            url = self._get_flac_url(metadata.id, token)

            # 3. Costruisci il percorso (forzato a .flac)
            dest = self._build_output_path(
                metadata,
                output_dir,
                filename_format,
                position,
                include_track_num,
                use_album_track_num,
                first_artist_only,
                extension=".flac"
            )

            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest), fmt="flac")

            # 4. Scarica il file FLAC
            self._http.stream_to_file(
                url,
                str(dest),
                progress_cb=self._progress_cb,
                extra_headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": _ORIGIN,
                    "Referer": f"{_ORIGIN}/",
                },
            )

            # 5. Valida il download (evita fake FLAC o preview da 30s)
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, err_msg, self.name)

            # 6. Recupera e formatta i tag di MusicBrainz
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

            qobuz_token = kwargs.get("qobuz_token", "") or os.environ.get("QOBUZ_AUTH_TOKEN", "")

            # 7. Incorpora tutti i metadati sul file FLAC
            embed_metadata(
                dest,
                metadata,
                first_artist_only=first_artist_only,
                cover_url=metadata.cover_url,
                session=self._http._session,
                extra_tags=mb_tags,
                embed_lyrics=embed_lyrics,
                lyrics_providers=lyrics_providers,
                lyrics_spotify_token=lyrics_spotify_token,
                enrich=enrich_metadata,
                enrich_providers=enrich_providers,
                enrich_qobuz_token=qobuz_token,
                is_album                = is_album,
            )

            return DownloadResult.ok(self.name, str(dest), fmt="flac")

        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))

        except Exception as exc:
            logger.exception("[%s] crash", self.name)
            return DownloadResult.fail(self.name, f"unexpected: {exc}")
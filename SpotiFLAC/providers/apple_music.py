from __future__ import annotations

import logging
import os
import difflib
import time
from pathlib import Path
import requests

from ..core.errors import SpotiflacError, ErrorKind, TrackNotFoundError
from ..core.http import RetryConfig
from ..core.models import TrackMetadata, DownloadResult
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags
from ..core.tagger import embed_metadata, _print_mb_summary, EmbedOptions
from ..core.provider_stats import record_success, record_failure
from ..core.download_validation import validate_downloaded_track
from ..core.console import print_source_banner, print_quality_fallback, print_api_failure
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# Endpoints per il proxy di download (basati su Zarz/app2)
_PROXY_DIRECT_URL = "https://api.zarz.moe/v1/dl/app2"
_PROXY_QUEUED_BASE = "https://api.zarz.moe/v1/dl/app"

class AppleMusicProvider(BaseProvider):
    name = "apple-music"

    def __init__(self, timeout_s: int = 30, proxy_api_key: str = "") -> None:
        super().__init__(timeout_s=timeout_s, retry=RetryConfig(max_attempts=2))
        self._session = self._http._session
        self._url_cache = {} # FIX: Sistema di caching in memoria introdotto

        headers = {
            "User-Agent": _DEFAULT_UA,
            "Accept": "application/json"
        }
        if proxy_api_key:
            headers["Authorization"] = f"Bearer {proxy_api_key}"
            headers["X-API-Key"] = proxy_api_key

        self._session.headers.update(headers)

    def _normalize_codec(self, quality: str) -> str:
        q = quality.lower()
        if q in ["alac", "atmos", "ac3", "aac", "aac-legacy"]:
            return q
        if q in ["high", "lossless"]:
            return "alac"
        return "aac"

    def _resolve_track_url(self, isrc: str) -> str | None:
        """
        Sfrutta l'API pubblica di iTunes per trovare l'URL della traccia.
        """
        try:
            url = f"https://itunes.apple.com/lookup?isrc={isrc}"
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("resultCount", 0) > 0:
                return data["results"][0].get("trackViewUrl")
        except Exception as e:
            logger.warning("[apple-music] Risoluzione URL tramite iTunes fallita per ISRC %s: %s", isrc, e)
        return None

    def _resolve_track_url_by_search(self, title: str, artists: str, isrc: str = "") -> str | None:
        import urllib.parse
        try:
            first_artist = artists.split(",")[0].strip()
            query = f"{title} {first_artist}"

            # Controllo cache
            cache_key = f"search_{query}_{isrc}"
            if cache_key in self._url_cache:
                return self._url_cache[cache_key]

            url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=song&limit=10"
            resp = self._session.get(url, timeout=15)
            results = resp.json().get("results", [])

            if not results: return None

            best_match = None
            best_score = -1

            for r in results:
                score = 0
                r_isrc = r.get("isrc", "")

                # Boost immenso se l'ISRC matcha (stessa logica del JS)
                if isrc and r_isrc and isrc.upper() == r_isrc.upper():
                    score += 100

                # Comparazione fuzzy di titolo e artista
                score += difflib.SequenceMatcher(None, title.lower(), r.get("trackName", "").lower()).ratio() * 60
                score += difflib.SequenceMatcher(None, first_artist.lower(), r.get("artistName", "").lower()).ratio() * 40

                if score > best_score:
                    best_score = score
                    best_match = r.get("trackViewUrl")

            self._url_cache[cache_key] = best_match
            return best_match

        except Exception as e:
            logger.debug("[apple-music] Ricerca testuale fallita: %s", e)
        return None

    def _get_stream_url(self, track_url: str, codec: str) -> tuple[str | None, str | None]:
        """
        Tenta prima il download diretto (app2). Se fallisce, ripiega su app in coda.
        Restituisce una tupla (api_utilizzata, stream_url).
        """
        req_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/"
        }

        # 1. Tentativo Diretto (App2)
        try:
            resp = self._session.post(
                _PROXY_DIRECT_URL,
                json={"url": track_url, "codec": codec},
                headers=req_headers,
                timeout=15
            )

            if resp.headers.get("cf-mitigated", "").lower() == "challenge":
                raise SpotiflacError(
                    ErrorKind.NETWORK_ERROR,
                    "Proxy bloccato da Cloudflare challenge",
                    self.name,
                )
            else:
                resp.raise_for_status()
                data = resp.json()
                if data.get("success") and data.get("stream_url"):
                    record_success(self.name, _PROXY_DIRECT_URL)
                    return _PROXY_DIRECT_URL, data["stream_url"]
        except requests.HTTPError as e:
            err_msg = e.response.json().get("error") if e.response.text else str(e)
            logger.debug("[apple-music] app2 rifiutato per %s: %s", codec, err_msg)
            record_failure(self.name, _PROXY_DIRECT_URL)
        except Exception as e:
            logger.debug("[apple-music] Fallback ad app2 non riuscito: %s", e)
            record_failure(self.name, _PROXY_DIRECT_URL)

        # 2. Tentativo in Coda (App)
        download_endpoint = f"{_PROXY_QUEUED_BASE}/download"
        try:
            resp = self._session.post(
                download_endpoint,
                json={"url": track_url, "codec": codec},
                headers=req_headers,
                timeout=15
            )

            if resp.headers.get("cf-mitigated", "").lower() == "challenge":
                return None, None

            resp.raise_for_status()
            job_data = resp.json()
            job_id = job_data.get("job_id")

            if not job_id:
                logger.warning("[apple-music] Nessun job_id restituito dal proxy di coda per %s.", codec)
                record_failure(self.name, download_endpoint)
                return None, None

            # Polling in attesa del completamento
            max_wait_s = 60 * 60
            deadline = time.time() + max_wait_s

            while time.time() < deadline:
                st_resp = self._session.get(f"{_PROXY_QUEUED_BASE}/status/{job_id}", timeout=15)
                st_resp.raise_for_status()
                st_data = st_resp.json()
                status = st_data.get("status", "").lower()

                if status == "completed":
                    record_success(self.name, _PROXY_QUEUED_BASE)
                    return _PROXY_QUEUED_BASE, f"{_PROXY_QUEUED_BASE}/file/{job_id}"
                elif status == "failed":
                    err = st_data.get('error', 'unknown error')
                    logger.warning("[apple-music] Errore API per codec %s: %s", codec, err)
                    record_failure(self.name, _PROXY_QUEUED_BASE)
                    return None, None

                time.sleep(2.5)

            logger.warning("[apple-music] Timeout nell'attesa della traccia per codec %s.", codec)
            record_failure(self.name, _PROXY_QUEUED_BASE)
            return None, None

        except Exception as e:
            logger.debug("[apple-music] Impossibile recuperare lo stream in coda per %s: %s", codec, e)
            record_failure(self.name, download_endpoint)
            return None, None


    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            quality:             str              = "alac",
            filename_format:     str              = "{title} - {artist}",
            position:            int              = 1,
            include_track_num:   bool             = False,
            use_album_track_num: bool             = False,
            first_artist_only:   bool             = False,
            allow_fallback:      bool             = True,
            embed_lyrics:        bool             = False,
            lyrics_providers:    list[str] | None = None,
            enrich_metadata:     bool             = False,
            enrich_providers:    list[str] | None = None,
            qobuz_token:         str | None       = None,
            is_album:            bool             = False,
            **kwargs,
    ) -> DownloadResult:

        is_native_apple = metadata.external_url and ("music.apple.com" in metadata.external_url or "apple.com" in metadata.external_url)

        if not metadata.isrc and not is_native_apple:
            return DownloadResult.fail(self.name, "Nessun ISRC o URL Apple Music fornito per la risoluzione.")

        try:
            target_codec = self._normalize_codec(quality)
            codecs_to_try = [target_codec]

            if allow_fallback:
                if target_codec == "atmos":
                    codecs_to_try.extend(["alac", "aac", "aac-legacy"])
                elif target_codec in ["alac", "ac3"]:
                    codecs_to_try.extend(["aac", "aac-legacy"])
                elif target_codec == "aac":
                    codecs_to_try.extend(["aac-legacy"])

                # Rimuove duplicati preservando l'ordine
                codecs_to_try = list(dict.fromkeys(codecs_to_try))

            # Trigger Asincrono MusicBrainz
            mb_fetcher = None
            if metadata.isrc:
                mb_fetcher = AsyncMBFetch(metadata.isrc)

            dest = self._build_output_path(
                metadata,
                output_dir,
                filename_format=filename_format,
                position=position,
                include_track_num=include_track_num,
                use_album_track_num=use_album_track_num,
                first_artist_only=first_artist_only,
                extension=".m4a"
            )

            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            # Risoluzione URL
            track_url = None
            if is_native_apple:
                track_url = metadata.external_url
            else:
                if metadata.isrc:
                    track_url = self._resolve_track_url(metadata.isrc)

                # FALLBACK: Se l'ISRC fallisce, cerca per Titolo e Artista
                if not track_url:
                    logger.debug("[apple-music] ISRC non trovato, tento la ricerca testuale...")
                    track_url = self._resolve_track_url_by_search(metadata.title, metadata.artists)

            if not track_url:
                raise TrackNotFoundError(self.name, f"Traccia non trovata (ISRC: {metadata.isrc})")

            logger.info("[apple-music] URL Traccia risolto: %s", track_url)

            stream_url = None
            used_codec = None
            api_used = None

            # Fallback Loop dei Codec
            for current_codec in codecs_to_try:
                logger.debug("[apple-music] Tentativo stream con codec: %s", current_codec)
                api_used, stream_url = self._get_stream_url(track_url, current_codec)
                if stream_url:
                    used_codec = current_codec
                    break
                logger.warning("[apple-music] Codec %s fallito, provo fallback...", current_codec)

            if not stream_url or not used_codec:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, "Nessuno stream audio disponibile (esauriti i fallback).", self.name)

            if used_codec != target_codec:
                print_quality_fallback("Apple Music", target_codec.upper(), used_codec.upper())

            print_source_banner("Apple Music", api_used or "Proxy", used_codec.upper())

            with self._session.get(stream_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(str(dest), "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(downloaded, total)
            # Validazione Traccia (Controllo File Corrotto/Tronco)
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                raise SpotiflacError(ErrorKind.FILE_IO, err_msg, self.name)

            mb_tags: dict[str, str] = {}
            res: dict = {}
            if mb_fetcher:
                res = mb_fetcher.future.result()

            mb_tags = mb_result_to_tags(res)

            _print_mb_summary(mb_tags)

            opts = EmbedOptions(
                first_artist_only    = first_artist_only,
                cover_url            = metadata.cover_url,
                embed_lyrics         = embed_lyrics,
                lyrics_providers     = lyrics_providers or [],
                enrich               = enrich_metadata,
                enrich_providers     = enrich_providers,
                enrich_qobuz_token   = qobuz_token or "",
                is_album             = is_album,
                extra_tags           = mb_tags,
            )
            embed_metadata(str(dest), metadata, opts, session=self._session)

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] Errore imprevisto", self.name)
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
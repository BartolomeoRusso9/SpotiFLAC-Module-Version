from __future__ import annotations

import asyncio
import difflib
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from ..core.console import print_quality_fallback, print_source_banner
from ..core.download_validation import validate_downloaded_track_async
from ..core.endpoints import get_apple_music_endpoint
from ..core.errors import ErrorKind, SpotiflacError, TrackNotFoundError
from ..core.http import RetryConfig
from ..core.models import DownloadResult, TrackMetadata
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags
from ..core.provider_stats import record_failure_async, record_success_async
from ..core.quality import normalize_quality
from ..core.tagger import EmbedOptions, _print_mb_summary, embed_metadata_async
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

class AppleMusicProvider(BaseProvider):
    name = "apple-music"
    # Allineato al default JS di downloadMaxWaitMinutes: 60 min
    MAX_POLLING_WAIT_S = 3600

    def __init__(self, timeout_s: int = 30, proxy_api_key: str = "") -> None:
        headers = {
            "User-Agent": _DEFAULT_UA,
            "Accept": "application/json"
        }
        if proxy_api_key:
            headers["Authorization"] = f"Bearer {proxy_api_key}"
            headers["X-API-Key"] = proxy_api_key

        super().__init__(timeout_s=timeout_s, retry=RetryConfig(max_attempts=2), headers=headers)

        # Cache per gli URL di ricerca
        self._url_cache: OrderedDict[str, str] = OrderedDict()
        self._cache_limit = 200

    def set_progress_callback(self, cb: Callable[[int, int], Awaitable[None] | None]) -> None:
        def safe_wrapper(written: int, total: int) -> None:
            if cb:
                res = cb(written, total)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
        
        super().set_progress_callback(safe_wrapper)
        self._progress_cb = safe_wrapper

    def _normalize_codec(self, quality: str) -> str:
        q = quality.lower()
        if q in ["alac", "atmos", "ac3", "aac", "aac-legacy"]:
            return q
        if q in ["high", "lossless"]:
            return "alac"
        return "aac"

    async def _resolve_track_url_async(self, isrc: str) -> str | None:
        """Uses l'API pubblica di iTunes per trovare l'URL della track delegando l'encoding all'AsyncClient httpx."""
        try:
            resp = await self._async_http.get(
                "https://itunes.apple.com/lookup",
                params={"isrc": isrc},
                timeout=15
            )
            data = resp.json()
            if data.get("resultCount", 0) > 0:
                return data["results"][0].get("trackViewUrl")
        except Exception as e:
            logger.warning("[apple-music] Risoluzione URL iTunes fallita per l'ISRC %s: %s", isrc, e)
        return None

    async def _resolve_track_url_by_search_async(self, title: str, artists: str, isrc: str = "", duration_ms: int = 0) -> str | None:
        try:
            first_artist = artists.split(",")[0].strip()
            query = f"{title} {first_artist}"
            cache_key = f"search_{query}_{isrc}"

            # Controllo cache LRU (operazione in memoria, non bloccante)
            if cache_key in self._url_cache:
                self._url_cache.move_to_end(cache_key)
                return self._url_cache[cache_key]

            resp = await self._async_http.get(
                "https://itunes.apple.com/search",
                params={"term": query, "entity": "song", "limit": 10},
                timeout=15
            )
            results = resp.json().get("results", [])

            if not results:
                return None

            best_match = None
            best_score = -1

            for r in results:
                score = 0
                r_isrc = r.get("isrc", "")

                if isrc and r_isrc and isrc.upper() == r_isrc.upper():
                    score += 100

                score += difflib.SequenceMatcher(None, title.lower(), r.get("trackName", "").lower()).ratio() * 50
                score += difflib.SequenceMatcher(None, first_artist.lower(), r.get("artistName", "").lower()).ratio() * 30

                # Controllo della durata (10 secondi di tolleranza)
                t_time = r.get("trackTimeMillis", 0)
                if duration_ms > 0 and t_time > 0:
                    if abs(duration_ms - t_time) <= 10000:
                        score += 20

                if score > best_score:
                    best_score = score
                    best_match = r.get("trackViewUrl")

            if best_match:
                self._url_cache[cache_key] = best_match
                if len(self._url_cache) > self._cache_limit:
                    try:
                        self._url_cache.popitem(last=False)
                    except KeyError:
                        pass

            return best_match

        except Exception as e:
            logger.debug("[apple-music] Ricerca testuale fallita: %s", e)
        return None

    async def _get_stream_url_async(self, track_url: str, codec: str) -> tuple[str | None, str | None]:
        """
        Tenta prima il download diretto (app2). Se fallisce, ripiega su app in coda.
        Returns una tupla (api_utilizzata, stream_url).
        """
        req_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/"
        }

        # 1. Tentativo Diretto (App2)
        proxy_direct = get_apple_music_endpoint("proxy_direct")
        try:
            resp = await self._async_http.post(
                proxy_direct,
                json={"url": track_url, "codec": codec},
                headers=req_headers,
                timeout=15
            )

            if resp.headers.get("cf-mitigated", "").lower() == "challenge":
                raise SpotiflacError(ErrorKind.NETWORK_ERROR, "Proxy bloccato da Cloudflare challenge", self.name)

            data = resp.json()
            if data.get("success") and data.get("stream_url"):
                await record_success_async(self.name, proxy_direct)
                return proxy_direct, data["stream_url"]

        except SpotiflacError as e:
            logger.debug("[apple-music] app2 rifiutato per %s: %s", codec, e)
            await record_failure_async(self.name, proxy_direct)
        except Exception as e:
            logger.debug("[apple-music] Fallback ad app2 fallito: %s", e)
            await record_failure_async(self.name, proxy_direct)

        # 2. Tentativo in Coda (App)
        _proxy_queued = get_apple_music_endpoint("proxy_queued")
        download_endpoint = f"{_proxy_queued}/download"
        try:
            resp = await self._async_http.post(
                download_endpoint,
                json={"url": track_url, "codec": codec},
                headers=req_headers,
                timeout=15
            )

            if resp.headers.get("cf-mitigated", "").lower() == "challenge":
                return None, None

            job_data = resp.json()
            job_id = job_data.get("job_id")

            if not job_id:
                logger.warning("[apple-music] Nessun job_id restituito dal proxy in coda per %s.", codec)
                await record_failure_async(self.name, download_endpoint)
                return None, None

            # Polling in attesa del completamento
            start_time = time.time()
            deadline = start_time + self.MAX_POLLING_WAIT_S
            poll_count = 0

            while time.time() < deadline:
                poll_count += 1
                if poll_count % 12 == 0:  # Ogni ~30 secondi
                    elapsed = int(time.time() - start_time)
                    print(f"  ⏳ Apple Music: in attesa del job {job_id[:8]}... ({elapsed}s trascorsi)")

                st_resp = await self._async_http.get(f"{_proxy_queued}/status/{job_id}", timeout=15)
                st_data = st_resp.json()
                status = st_data.get("status", "").lower()

                if status == "completed":
                    await record_success_async(self.name, get_apple_music_endpoint("proxy_queued"))
                    return _proxy_queued, f"{_proxy_queued}/file/{job_id}"

                if status == "failed":
                    err = st_data.get('error', 'Error API sconosciuto')
                    logger.warning("[apple-music] Error API proxy per codec %s: %s", codec, err)
                    await record_failure_async(self.name, get_apple_music_endpoint("proxy_queued"))
                    return None, None

                # Cruciale: Usiamo asyncio.sleep, NON time.sleep, per non bloccare l'Event Loop
                await asyncio.sleep(2.5)

            logger.warning("[apple-music] Timeout while waiting for track with codec %s.", codec)
            await record_failure_async(self.name, get_apple_music_endpoint("proxy_queued"))
            return None, None

        except Exception as e:
            logger.debug("[apple-music] Unable to retrievesre lo stream in coda per %s: %s", codec, e)
            await record_failure_async(self.name, download_endpoint)
            return None, None

    async def download_track_async(
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
            **kwargs:            Any,
    ) -> DownloadResult:

        is_native_apple = metadata.external_url and ("music.apple.com" in metadata.external_url or "apple.com" in metadata.external_url)

        if not metadata.isrc and not is_native_apple:
            return DownloadResult.fail(self.name, "Nessun ISRC o URL Apple Music fornito per la risoluzione.")

        try:
            nq = normalize_quality(quality)
            if nq == "DOLBY_ATMOS":
                target_codec = "atmos"
            elif nq in ("HI_RES_LOSSLESS", "HI_RES", "LOSSLESS"):
                target_codec = "alac"
            elif nq in ("HIGH", "LOW"):
                target_codec = "aac"
            else:
                target_codec = self._normalize_codec(quality)
            codecs_to_try = [target_codec]

            if allow_fallback:
                if target_codec == "atmos":
                    codecs_to_try.extend(["alac", "aac", "aac-legacy"])
                elif target_codec in ["alac", "ac3"]:
                    codecs_to_try.extend(["aac", "aac-legacy"])
                elif target_codec == "aac":
                    codecs_to_try.extend(["aac-legacy"])

                # Removes duplicati preservando l'ordine
                codecs_to_try = list(dict.fromkeys(codecs_to_try))

            # Trigger Asincrono MusicBrainz
            import concurrent.futures
            from ..core.isrc_utils import normalize_isrc
            _isrc_for_mb = normalize_isrc(getattr(metadata, "isrc", None) or "")
            logger.debug("[apple-music] ISRC at MB lookup: %r", _isrc_for_mb)
            mb_fetcher = AsyncMBFetch(_isrc_for_mb) if _isrc_for_mb else None
            if not mb_fetcher:
                logger.warning("[apple-music] MusicBrainz skipped: no valid ISRC available")

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
                return DownloadResult.skipped_result(self.name, str(dest), fmt="m4a")

            # Risoluzione URL Asincrona
            track_url = None
            if is_native_apple:
                track_url = metadata.external_url
            else:
                if metadata.isrc:
                    track_url = await self._resolve_track_url_async(metadata.isrc)

                # FALLBACK: Se l'ISRC fallisce, cerca per Titolo, Artist, ISRC e durata
                if not track_url:
                    logger.debug("[apple-music] ISRC not found, tentativo tramite ricerca testuale...")
                    track_url = await self._resolve_track_url_by_search_async(
                        metadata.title,
                        metadata.artists,
                        metadata.isrc or "",
                        metadata.duration_ms,
                    )

            if not track_url:
                raise TrackNotFoundError(self.name, f"Track not found (ISRC: {metadata.isrc})")

            logger.info("[apple-music] Resolved track URL: %s", track_url)

            stream_url = None
            used_codec = None
            api_used = None

            # Fallback Loop dei Codec Asincrono
            for current_codec in codecs_to_try:
                logger.debug("[apple-music] Tentativo stream con codec: %s", current_codec)
                api_used, stream_url = await self._get_stream_url_async(track_url, current_codec)
                if stream_url:
                    used_codec = current_codec
                    break
                logger.warning("[apple-music] Codec %s fallito, tentativo di fallback in corso...", current_codec)

            if not stream_url or not used_codec:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, "Nessuno stream audio disponibile (fallback esauriti).", self.name)

            if used_codec != target_codec:
                print_quality_fallback("Apple Music", target_codec.upper(), used_codec.upper())

            print_source_banner("Apple Music", "", used_codec.upper())

            # Download su disco via Async Client
            await self._async_http.stream_to_file(stream_url, str(dest), self._progress_cb)

            # Validazione Track Async (Controllo File Corrotto/Tronco)
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = await validate_downloaded_track_async(str(dest), expected_s)
            if not valid:
                raise SpotiflacError(ErrorKind.FILE_IO, err_msg, self.name)

            mb_tags: dict[str, str] = {}
            res: dict[str, Any] = {}
            if mb_fetcher:
                try:
                    res = await asyncio.to_thread(lambda: mb_fetcher.future.result(timeout=12))
                    mb_tags = mb_result_to_tags(res)
                    if mb_tags:
                        logger.info("[apple-music] MusicBrainz tags found: %s", list(mb_tags.keys()))
                        _print_mb_summary(mb_tags)
                    else:
                        logger.warning("[apple-music] MusicBrainz returned no tags (ISRC: %r)", _isrc_for_mb)
                except concurrent.futures.TimeoutError:
                    logger.warning("[apple-music] MusicBrainz timed out after 12s, skipping MB tags")
                except Exception as exc:
                    logger.warning("[apple-music] MusicBrainz error: %s", exc)

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
            
            # Embed Asyncrono
            await embed_metadata_async(str(dest), metadata, opts, session=await self._async_http._client())

            return DownloadResult.ok(self.name, str(dest), fmt="m4a")

        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] Error inaspettato", self.name)
            return DownloadResult.fail(self.name, f"Inaspettato: {exc}")
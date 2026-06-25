# youtube_provider.py
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, urlparse

import httpx
import yt_dlp

from ..core.download_validation import validate_downloaded_track_async
from ..core.endpoints import get_youtube_endpoints
from ..core.errors import SpotiflacError
from ..core.models import DownloadResult, TrackMetadata
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags
from ..core.tagger import EmbedOptions, embed_metadata_async
from .base import BaseProvider

logger = logging.getLogger(__name__)


def _shorten_api_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

YT_SEARCH_PARAMS_TRACKS = "EgWKAQIIAQ=="
INNERTUBE_CLIENT_VERSION = "1.20240801.01.00"


class YouTubeProvider(BaseProvider):
    name = "youtube"
    _is_async = True

    def __init__(self, timeout_s: int = 120) -> None:
        super().__init__(timeout_s=timeout_s, headers={"User-Agent": _DEFAULT_UA})
        self._enrichment_cache: dict[str, dict] = {}

    async def get_url_async(self, url: str) -> tuple[str, list[TrackMetadata]]:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)

        playlist_id = qs.get("list", [None])[0]
        if not playlist_id and "/playlist" in parsed.path:
            playlist_id = qs.get("list", [None])[0]

        if "/browse/" in parsed.path:
            browse_id = parsed.path.split("/browse/")[1].split("?")[0]
            return await self._fetch_container_async(browse_id)

        if playlist_id:
            browse_id = (
                playlist_id
                if playlist_id.startswith(("VL", "PL"))
                else f"VL{playlist_id}"
            )
            return await self._fetch_container_async(browse_id)

        video_id = self._extract_video_id(url)
        if video_id:
            meta = await self._get_single_track_metadata_async(video_id)
            return meta.title, [meta]

        raise ValueError(f"URL YouTube non supportato o non riconosciuto: {url}")

    async def _get_single_track_metadata_async(self, video_id: str) -> TrackMetadata:
        url = "https://music.youtube.com/youtubei/v1/player?alt=json"
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": INNERTUBE_CLIENT_VERSION,
                }
            },
            "videoId": video_id,
        }

        resp = await self._async_http.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        details = data.get("videoDetails", {})
        title = details.get("title", "Unknown")
        artist = details.get("author", "Unknown Artist")
        duration = int(details.get("lengthSeconds") or 0) * 1000

        thumbs = details.get("thumbnail", {}).get("thumbnails", [])
        cover_url = thumbs[-1].get("url") if thumbs else ""

        return TrackMetadata(
            id=video_id,
            title=title,
            artists=artist,
            album_artist=artist,
            album="YouTube",
            duration_ms=duration,
            cover_url=cover_url,
            external_url=f"https://music.youtube.com/watch?v={video_id}",
            extra_info={"provider": "youtube"},
        )

    async def _fetch_container_async(
        self, browse_id: str
    ) -> tuple[str, list[TrackMetadata]]:
        logger.info("[youtube] Fetching container: %s", browse_id)

        url = "https://music.youtube.com/youtubei/v1/browse?alt=json"
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": INNERTUBE_CLIENT_VERSION,
                }
            },
            "browseId": browse_id,
        }

        resp = await self._async_http.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        title = "Unknown YouTube Container"
        header_renderer = data.get("header", {}).get("musicDetailHeaderRenderer", {})
        runs = header_renderer.get("title", {}).get("runs", [])
        if runs and isinstance(runs, list):
            title = runs[0].get("text", title)

        tracks: list[TrackMetadata] = []
        self._parse_tracks_from_data(data, tracks)

        continuation = self._get_continuation_token(data)
        while continuation:
            logger.debug("[youtube] Fetching continuation...")
            cont_data = await self._fetch_continuation_async(continuation)
            if not cont_data:
                break

            added = self._parse_tracks_from_data(cont_data, tracks)
            if added == 0:
                break

            continuation = self._get_continuation_token(cont_data)

        for i, track in enumerate(tracks):
            track.track_number = i + 1

        return title, tracks

    def _parse_tracks_from_data(
        self, data: dict, track_list: list[TrackMetadata]
    ) -> int:
        count_before = len(track_list)
        items = self._find_key_recursive(data, "musicResponsiveListItemRenderer")

        for item in items:
            if not isinstance(item, dict):
                continue

            v_id = item.get("playlistItemData", {}).get("videoId")
            if not v_id:
                continue

            columns = item.get("flexColumns", [])
            title = "Unknown"
            artist = "Unknown Artist"

            if columns and isinstance(columns, list):
                first_col = columns[0].get(
                    "musicResponsiveListItemFlexColumnRenderer", {}
                )
                runs = first_col.get("text", {}).get("runs", [])
                if runs:
                    title = runs[0].get("text", "Unknown")

                if len(columns) > 1:
                    second_col = columns[1].get(
                        "musicResponsiveListItemFlexColumnRenderer", {}
                    )
                    artist_runs = second_col.get("text", {}).get("runs", [])
                    artist_parts = [
                        r["text"]
                        for r in artist_runs
                        if "browseId"
                        in r.get("navigationEndpoint", {}).get("browseEndpoint", {})
                    ]
                    if artist_parts:
                        artist = ", ".join(artist_parts)

            thumbnails = (
                item.get("thumbnail", {})
                .get("musicThumbnailRenderer", {})
                .get("thumbnail", {})
                .get("thumbnails", [])
            )
            cover = thumbnails[-1].get("url") if thumbnails else ""

            track_list.append(
                TrackMetadata(
                    id=v_id,
                    title=title,
                    artists=artist,
                    album_artist=artist,
                    album="YouTube Music",
                    duration_ms=0,
                    cover_url=cover,
                    external_url=f"https://music.youtube.com/watch?v={v_id}",
                    extra_info={"provider": "youtube"},
                )
            )

        return len(track_list) - count_before

    def _get_continuation_token(self, data: dict) -> str | None:
        tokens = self._find_key_recursive(data, "continuation")
        return next(tokens, None)

    async def _fetch_continuation_async(self, token: str) -> dict | None:
        url = f"https://music.youtube.com/youtubei/v1/browse?alt=json&ctoken={quote(token)}&continuation={quote(token)}"
        try:
            resp = await self._async_http.post(
                url,
                json={
                    "context": {
                        "client": {
                            "clientName": "WEB_REMIX",
                            "clientVersion": INNERTUBE_CLIENT_VERSION,
                        }
                    }
                },
                timeout=10,
            )
            return resp.json() if resp.is_success else None
        except httpx.RequestError as e:
            logger.debug(f"[youtube] Error di rete in _fetch_continuation: {e}")
            return None

    def _find_key_recursive(self, data: Any, key: str) -> Iterator[Any]:
        if isinstance(data, dict):
            for k, v in data.items():
                if k == key:
                    yield v
                else:
                    yield from self._find_key_recursive(v, key)
        elif isinstance(data, list):
            for item in data:
                yield from self._find_key_recursive(item, key)

    async def _enrich_metadata_with_odesli_async(
        self, metadata: TrackMetadata, platform_url: str
    ) -> str | None:
        api_url = f"https://api.song.link/v1-alpha.1/links?url={quote(platform_url)}"
        try:
            resp = await self._async_http.get(api_url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                links = data.get("linksByPlatform", {})

                entities = data.get("entitiesByUniqueId", {})
                for entity_data in entities.values():
                    if not metadata.isrc and entity_data.get("isrc"):
                        try:
                            from ..core.isrc_utils import normalize_isrc

                            isrc_val = normalize_isrc(entity_data.get("isrc"))
                            if isrc_val:
                                try:
                                    from ..core.isrc_utils import (
                                        confirm_isrc_with_qobuz_async,
                                    )

                                    ok, _ = await confirm_isrc_with_qobuz_async(
                                        isrc_val,
                                        metadata.title or "",
                                        metadata.artists or "",
                                        metadata.duration_ms or 0,
                                    )
                                    if ok:
                                        metadata.isrc = isrc_val
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    logger.info(f"[youtube] ISRC found via Odesli: {metadata.isrc}")

                if not metadata.isrc and "deezer" in links:
                    deezer_url = links["deezer"].get("url", "")
                    match = re.search(r"/track/(\d+)", deezer_url)
                    if match:
                        deezer_id = match.group(1)
                        try:
                            dz_resp = await self._async_http.get(
                                f"https://api.deezer.com/track/{deezer_id}", timeout=10
                            )
                            if dz_resp.status_code == 200:
                                dz_data = dz_resp.json()
                                if dz_data.get("isrc"):
                                    try:
                                        from ..core.isrc_utils import normalize_isrc

                                        isrc_val = normalize_isrc(dz_data.get("isrc"))
                                        if isrc_val:
                                            try:
                                                from ..core.isrc_utils import (
                                                    confirm_isrc_with_qobuz_async,
                                                )

                                                ok, _ = (
                                                    await confirm_isrc_with_qobuz_async(
                                                        isrc_val,
                                                        metadata.title or "",
                                                        metadata.artists or "",
                                                        metadata.duration_ms or 0,
                                                    )
                                                )
                                                if ok:
                                                    metadata.isrc = isrc_val
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                        except httpx.RequestError as e:
                            logger.warning(
                                f"[youtube] Deezer API fallback network error: {e}"
                            )

                yt_info = links.get("youtubeMusic") or links.get("youtube")
                if yt_info and yt_info.get("url"):
                    return yt_info["url"]

        except Exception as exc:
            logger.warning(f"[youtube] Odesli API enrichTrack failed: {exc}")
        return None

    async def _get_youtube_url_async(self, metadata: TrackMetadata) -> str:
        if metadata.external_url:
            platform_url = metadata.external_url
        elif metadata.id.startswith("tidal_"):
            platform_url = (
                f"https://tidal.com/browse/track/{metadata.id.removeprefix('tidal_')}"
            )
        elif metadata.id.startswith("spotify:"):
            platform_url = (
                f"https://open.spotify.com/track/{metadata.id.split(':')[-1]}"
            )
        else:
            platform_url = f"https://song.link/s/{metadata.id}"

        yt_url = await self._enrich_metadata_with_odesli_async(metadata, platform_url)
        if yt_url:
            return yt_url

        if metadata.title and metadata.artists:
            yt_url = await self._search_youtube_direct_async(
                metadata.title, metadata.artists
            )
            if yt_url:
                return yt_url

        raise RuntimeError("Failed to resolve YouTube URL")

    async def _search_youtube_direct_async(
        self, track_name: str, artist_name: str
    ) -> str | None:
        query = f"{track_name} {artist_name}"
        url = "https://music.youtube.com/youtubei/v1/search?alt=json"

        payload = {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": INNERTUBE_CLIENT_VERSION,
                }
            },
            "query": query,
            "params": YT_SEARCH_PARAMS_TRACKS,
        }

        try:
            resp = await self._async_http.post(
                url, json=payload, headers={"User-Agent": _DEFAULT_UA}, timeout=10
            )
            resp.raise_for_status()

            data = resp.json()
            first_vid = next(self._find_key_recursive(data, "videoId"), None)

            if first_vid:
                return f"https://music.youtube.com/watch?v={first_vid}"
        except Exception as exc:
            logger.warning(f"[youtube] Direct search failed: {exc}")

        return None

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        match = re.search(r"(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})", url)
        return match.group(1) if match else None

    def _download_direct_innertube(self, video_id: str, dest_path: str) -> bool:
        def _yt_dlp_progress(d: dict) -> None:
            if d.get("status") == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                if self._progress_cb and total > 0:
                    self._progress_cb(downloaded, total)

        class MuteLogger:
            def debug(self, msg: str) -> None:
                pass

            def info(self, msg: str) -> None:
                pass

            def warning(self, msg: str) -> None:
                pass

            def error(self, msg: str) -> None:
                pass

        out_tmpl = str(Path(dest_path).with_suffix(".%(ext)s"))

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": MuteLogger(),
            "outtmpl": out_tmpl,
            "extractor_args": {
                "youtube": [
                    "player_client=android,mweb,ios",
                    "player_skip=webpage,configs,js",
                ]
            },
            "updatetime": False,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "256",
                }
            ],
            "progress_hooks": [_yt_dlp_progress],
        }

        url = f"https://www.youtube.com/watch?v={video_id}"

        try:
            logger.info(f"[youtube] Native yt-dlp download (ID: {video_id})...")
            if os.path.exists(dest_path):
                os.remove(dest_path)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if os.path.exists(dest_path):
                return True
        except Exception as e:
            # The DownloadSuccessfullyStarted exception will bubble up here from the thread
            # Let it propagate to the upper block if it is ours!
            if "DownloadSuccessfullyStarted" in str(e):
                raise e
            logger.warning(f"[youtube] yt-dlp error: {e}")

        return False

    async def _request_cobalt_async(self, video_url: str) -> str | None:
        cobalt_instances = get_youtube_endpoints("cobalt")
        if not isinstance(cobalt_instances, list):
            cobalt_instances = [cobalt_instances] if cobalt_instances else []

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _DEFAULT_UA,
        }

        for base_url in cobalt_instances:
            try:
                payload_v10 = {
                    "url": video_url,
                    "downloadMode": "audio",
                    "audioFormat": "mp3",
                }
                zarz_data = get_youtube_endpoints("zarz_clean")
                api_url = (
                    (zarz_data[0] if zarz_data else base_url)
                    if isinstance(zarz_data, list)
                    else (zarz_data or base_url)
                )

                resp = await self._async_http.post(
                    api_url, json=payload_v10, headers=headers, timeout=10
                )

                if resp.status_code == 404:
                    payload_v7 = {
                        "url": video_url,
                        "isAudioOnly": True,
                        "aFormat": "mp3",
                    }
                    api_url = f"{base_url.rstrip('/')}/api/json"
                    resp = await self._async_http.post(
                        api_url, json=payload_v7, headers=headers, timeout=10
                    )

                if resp.status_code in (200, 202):
                    data = resp.json()
                    dl_url = (
                        data.get("url") or data.get("audio") or data.get("audioUrl")
                    )
                    if dl_url:
                        return dl_url
            except Exception as exc:
                logger.debug(
                    f"[youtube] Fallimento Cobalt su {_shorten_api_url(base_url)}: {exc}"
                )

        return None

    async def _request_yt1d_async(self, video_url: str) -> str | None:
        try:
            res_config = await self._async_http.get(
                "https://yt1d.io/results/",
                headers={"User-Agent": _DEFAULT_UA},
                timeout=10,
            )
            nonce_match = re.search(r'"nonce"\s*:\s*"([^"]+)"', res_config.text)
            if not nonce_match:
                return None

            payload = {
                "action": "process_youtube_audio_download",
                "video_url": video_url,
                "quality": "m4a",
                "nonce": nonce_match.group(1),
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://yt1d.io",
                "Referer": "https://yt1d.io/results/",
                "User-Agent": _DEFAULT_UA,
            }

            res_audio = await self._async_http.post(
                "https://yt1d.io/wp-admin/admin-ajax.php",
                data=payload,
                headers=headers,
                timeout=15,
            )
            if res_audio.status_code == 200:
                data = res_audio.json()
                dl_url = (
                    data.get("downloadUrl")
                    or data.get("downloadURL")
                    or data.get("url")
                )
                if not dl_url and data.get("data"):
                    nested = data["data"]
                    dl_url = (
                        nested.get("downloadUrl")
                        or nested.get("downloadURL")
                        or nested.get("url")
                    )

                if dl_url and dl_url.startswith("http"):
                    return dl_url
        except Exception as exc:
            logger.warning(f"[youtube] yt1d fallback failed: {exc}")
        return None

    async def download_track_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        quality: str = "320",
        filename_format: str = "{title} - {artist}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        allow_fallback: bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        qobuz_token: str | None = None,
        is_album: bool = False,
        **kwargs,
    ) -> DownloadResult:
        try:
            dest = self._build_output_path(
                metadata,
                output_dir,
                filename_format=filename_format,
                position=position,
                include_track_num=include_track_num,
                use_album_track_num=use_album_track_num,
                first_artist_only=first_artist_only,
                extension=".m4a",
            )
            if self._file_exists(dest):
                return DownloadResult.skipped_result(self.name, str(dest), fmt="m4a")

            is_native_yt = metadata.extra_info.get("provider") == "youtube"
            looks_like_yt_id = len(metadata.id) == 11 and not metadata.id.startswith(
                "spotify:"
            )

            if is_native_yt or looks_like_yt_id:
                video_id, yt_url = (
                    metadata.id,
                    f"https://www.youtube.com/watch?v={metadata.id}",
                )
            else:
                yt_url = await self._get_youtube_url_async(metadata)
                video_id = self._extract_video_id(yt_url)

            if not video_id:
                return DownloadResult.fail(self.name, "Could not extract video ID")

            import concurrent.futures
            from ..core.isrc_utils import normalize_isrc

            _isrc_for_mb = normalize_isrc(getattr(metadata, "isrc", None) or "")
            logger.debug("[youtube] ISRC at MB lookup: %r", _isrc_for_mb)
            mb_fetcher = AsyncMBFetch(_isrc_for_mb) if _isrc_for_mb else None
            if not mb_fetcher:
                logger.warning("[youtube] MusicBrainz skipped: no valid ISRC available")

            try:
                from ..core.console import print_source_banner

                print_source_banner("youtube", "music.youtube.com", "M4A 256kbps")
            except ImportError:
                pass

            download_success = False

            try:
                if await asyncio.to_thread(
                    self._download_direct_innertube, video_id, str(dest)
                ):
                    download_success = True
            except Exception as e:
                if "DownloadSuccessfullyStarted" in str(e):
                    # Ripropaghiamo l'errore fittizio allo script di test esterno!
                    raise e
                logger.warning(f"[youtube] yt-dlp fallito in thread: {e}")

            if not download_success:
                download_sources = [
                    ("Cobalt", self._request_cobalt_async),
                    ("YT1D", self._request_yt1d_async),
                ]
                for source_name, get_url_func in download_sources:
                    dl_url = await get_url_func(yt_url)
                    if not dl_url:
                        continue

                    logger.info(f"[youtube] Attempting download via {source_name}...")
                    try:
                        await self._async_http.stream_to_file(
                            dl_url,
                            str(dest),
                            self._progress_cb,
                            extra_headers={"User-Agent": _DEFAULT_UA},
                        )
                        download_success = True
                        break
                    except Exception as e:
                        # If it is our exception, re-raise it.
                        if "DownloadSuccessfullyStarted" in str(e):
                            raise e
                        logger.warning(
                            f"[youtube] Download via {source_name} failed: {e}"
                        )
                        if os.path.exists(str(dest)):
                            try:
                                os.remove(str(dest))
                            except OSError:
                                pass

            if not download_success:
                return DownloadResult.fail(
                    self.name, "All YouTube download sources failed"
                )

            expected_s = metadata.duration_ms // 1000
            valid, err_msg = await validate_downloaded_track_async(
                str(dest), expected_s
            )
            if not valid:
                return DownloadResult.fail(self.name, f"Validazione fallita: {err_msg}")

            mb_tags = {}
            if mb_fetcher:
                try:
                    res = await asyncio.to_thread(
                        lambda: mb_fetcher.future.result(timeout=12)
                    )
                    mb_tags = mb_result_to_tags(res)
                    if mb_tags:
                        logger.info(
                            "[youtube] MusicBrainz tags found: %s", list(mb_tags.keys())
                        )
                    else:
                        logger.warning(
                            "[youtube] MusicBrainz returned no tags (ISRC: %r)",
                            _isrc_for_mb,
                        )
                    if res:
                        mapping = {
                            "mbid_track": "MUSICBRAINZ_TRACKID",
                            "mbid_album": "MUSICBRAINZ_ALBUMID",
                            "mbid_artist": "MUSICBRAINZ_ARTISTID",
                            "mbid_relgroup": "MUSICBRAINZ_RELEASEGROUPID",
                            "mbid_albumartist": "MUSICBRAINZ_ALBUMARTISTID",
                            "barcode": "BARCODE",
                            "label": "LABEL",
                            "organization": "ORGANIZATION",
                            "country": "RELEASECOUNTRY",
                            "script": "SCRIPT",
                            "status": "RELEASESTATUS",
                            "media": "MEDIA",
                            "type": "RELEASETYPE",
                            "artist_sort": "ARTISTSORT",
                            "albumartist_sort": "ALBUMARTISTSORT",
                            "catalognumber": "CATALOGNUMBER",
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
                except concurrent.futures.TimeoutError:
                    logger.warning(
                        "[youtube] MusicBrainz timed out after 12s, skipping MB tags"
                    )
                except Exception as exc:
                    logger.warning("[youtube] MusicBrainz error: %s", exc)

            opts = EmbedOptions(
                first_artist_only=first_artist_only,
                cover_url=metadata.cover_url,
                extra_tags=mb_tags,
                embed_lyrics=embed_lyrics,
                lyrics_providers=lyrics_providers or [],
                enrich=enrich_metadata,
                enrich_providers=enrich_providers,
                enrich_qobuz_token=qobuz_token or "",
                is_album=is_album,
            )
            await embed_metadata_async(str(dest), metadata, opts)

            return DownloadResult.ok(self.name, str(dest), fmt="m4a")

        except SpotiflacError as exc:
            logger.error(f"[youtube] {exc}")
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            if "DownloadSuccessfullyStarted" in str(exc):
                raise exc
            logger.exception("[youtube] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

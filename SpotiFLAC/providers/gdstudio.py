from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..core.console import print_source_banner
from ..core.download_validation import validate_downloaded_track_async
from ..core.endpoints import get_asian_provider_endpoint
from ..core.errors import ErrorKind, SpotiflacError, TrackNotFoundError
from ..core.flac_validation import validate_and_repair_if_needed
from ..core.models import DownloadResult, TrackMetadata
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags
from ..core.tagger import EmbedOptions, embed_metadata_async
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class GDStudioProvider(BaseProvider):
    """Generic provider for GD Studio-based Asian sources.

    Subclasses should call super().__init__(timeout_s=...) and set
    `self._source` to the appropriate source string (e.g. 'netease').
    """

    def __init__(self, source: str, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        self._source = source
        self._async_http._headers.update({"User-Agent": _DEFAULT_UA})

    # Basic helpers shared across Netease/Kuwo/JOOX/Migu
    async def _search_async(self, query: str, count: int = 10) -> list[dict]:
        try:
            resp = await self._async_http.get(
                get_asian_provider_endpoint(self._source, "gdstudio"),
                params={
                    "types": "search",
                    "source": self._source,
                    "name": query,
                    "count": count,
                    "pages": 1,
                },
                timeout=10.0,
            )
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("data", data.get("result", []))
        except Exception as exc:
            logger.debug("[%s] Async search failed for '%s': %s", self._source, query, exc)
        return []

    async def _get_stream_async(self, track_id: str, requested_quality: int | None = None) -> tuple[str, int]:
        try:
            params = {"types": "url", "source": self._source, "id": track_id}
            if requested_quality:
                params["br"] = requested_quality
            resp = await self._async_http.get(
                get_asian_provider_endpoint(self._source, "gdstudio"),
                params=params,
                timeout=10.0,
            )
            data = resp.json()
            url = data.get("url", "")
            actual_br = int(data.get("br", 0)) if isinstance(data.get("br", 0), (int, str)) else 0
            return url, actual_br
        except Exception as exc:
            logger.debug("[%s] Async stream fetch failed for id=%s: %s", self._source, track_id, exc)
        return "", 0

    async def _get_pic_url_async(self, pic_id: str, size: int = 500) -> str:
        if not pic_id:
            return ""
        try:
            resp = await self._async_http.get(
                get_asian_provider_endpoint(self._source, "gdstudio"),
                params={"types": "pic", "source": self._source, "id": pic_id, "size": size},
                timeout=8.0,
            )
            return resp.json().get("url", "")
        except Exception as exc:
            logger.debug("[%s] Async pic fetch failed for pic_id=%s: %s", self._source, pic_id, exc)
        return ""

    async def _get_lyric_async(self, lyric_id: str) -> str:
        if not lyric_id:
            return ""
        try:
            resp = await self._async_http.get(
                get_asian_provider_endpoint(self._source, "gdstudio"),
                params={"types": "lyric", "source": self._source, "id": lyric_id},
                timeout=8.0,
            )
            return resp.json().get("lyric", "")
        except Exception as exc:
            logger.debug("[%s] Async lyric fetch failed for id=%s: %s", self._source, lyric_id, exc)
        return ""

    async def _get_album_tracks_async(self, album_id: str) -> list[dict]:
        try:
            resp = await self._async_http.get(
                get_asian_provider_endpoint(self._source, "gdstudio"),
                params={"types": "search", "source": f"{self._source}_album", "name": album_id, "count": 100, "pages": 1},
                timeout=12.0,
            )
            data = resp.json()
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.debug("[%s] Async album tracks fetch failed for id=%s: %s", self._source, album_id, exc)
        return []

    async def _item_to_metadata_async(self, item: dict, position: int = 1) -> TrackMetadata:
        track_id = str(item.get("id", ""))
        title = item.get("name", "Unknown")
        raw_artists = item.get("artist", [])
        if isinstance(raw_artists, list):
            artist_str = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in raw_artists).strip(", ") or "Unknown"
        else:
            artist_str = str(raw_artists) or "Unknown"
        album = item.get("album", "Unknown")
        pic_id = str(item.get("pic_id", ""))
        cover_url = await self._get_pic_url_async(pic_id) if pic_id else ""
        return TrackMetadata(
            id = f"{self._source}_{track_id}",
            title = title,
            artists = artist_str,
            album = album,
            album_artist = artist_str,
            duration_ms = 0,
            cover_url = cover_url,
            external_url = "",
            extra_info = {"provider": self._source, "raw_track_id": track_id, "pic_id": pic_id, "lyric_id": str(item.get("lyric_id", track_id))},
        )

    # Generic get_url / download_track reuse the same logic used previously in individual modules
    async def get_url_async(self, url: str) -> tuple[str, list[TrackMetadata]]:
        match = re.search(r"(\d{5,})", url)
        if match and "_album" in url.lower():
            album_id = match.group(1)
            items = await self._get_album_tracks_async(album_id)
            if items:
                tracks = [await self._item_to_metadata_async(it, i+1) for i, it in enumerate(items)]
                return tracks[0].album if tracks else "Unknown Album", tracks

        if match:
            track_id = match.group(1)
            items = await self._search_async(track_id, count=1)
            if items:
                meta = await self._item_to_metadata_async(items[0])
                return meta.title, [meta]

        query = url.strip()
        items = await self._search_async(query, count=20)
        if not items:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"No results for: {query}", self.name)
        tracks = [await self._item_to_metadata_async(it, i+1) for i, it in enumerate(items)]
        return f"Search: {query}", tracks

    async def download_track_async(self, metadata: TrackMetadata, output_dir: str, **kwargs: Any) -> DownloadResult:
        try:
            extra = metadata.extra_info or {}
            raw_track_id = extra.get("raw_track_id", "")
            if not raw_track_id:
                query = f"{metadata.title} {metadata.first_artist}".strip()
                items = await self._search_async(query, count=5)
                if not items:
                    raise TrackNotFoundError(self.name, f"Track not found on {self._source}: {query}")
                raw_track_id = str(items[0].get("id", ""))
                extra = {"raw_track_id": raw_track_id, "pic_id": str(items[0].get("pic_id", "")), "lyric_id": str(items[0].get("lyric_id", raw_track_id))}

            dl_url, actual_br = await self._get_stream_async(raw_track_id)
            if not dl_url:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, f"No lossless stream available on {self._source} for id={raw_track_id}", self.name)

            dest = self._build_output_path(metadata, output_dir, kwargs.get("filename_format", "{title} - {artist}"), kwargs.get("position", 1), kwargs.get("include_track_num", False), kwargs.get("use_album_track_num", False), kwargs.get("first_artist_only", False), extension=".flac")
            if self._file_exists(dest):
                return DownloadResult.skipped_result(self.name, str(dest), fmt="flac")

            import concurrent.futures
            from ..core.isrc_utils import normalize_isrc
            _isrc_for_mb = normalize_isrc(getattr(metadata, "isrc", None) or "")
            logger.debug("[%s] ISRC at MB lookup: %r", self._source, _isrc_for_mb)
            mb_fetcher = AsyncMBFetch(_isrc_for_mb) if _isrc_for_mb else None
            if not mb_fetcher:
                logger.warning("[%s] MusicBrainz skipped: no valid ISRC available", self._source)
            print_source_banner(self._source, "", "FLAC")
            await self._async_http.stream_to_file(dl_url, str(dest), self._progress_cb, extra_headers={"User-Agent": _DEFAULT_UA})

            expected_s = metadata.duration_ms // 1000
            valid, err_msg = await validate_downloaded_track_async(str(dest), expected_s)
            if not valid:
                if dest.exists():
                    os.remove(str(dest))
                return DownloadResult.fail(self.name, f"Validation failed: {err_msg}")

            gd_lyrics = None
            if kwargs.get("embed_lyrics") and extra.get("lyric_id"):
                gd_lyrics = await self._get_lyric_async(extra.get("lyric_id"))

            mb_tags = {}
            if mb_fetcher:
                try:
                    res = await asyncio.to_thread(lambda: mb_fetcher.future.result(timeout=12))
                    mb_tags = mb_result_to_tags(res)
                    if mb_tags:
                        logger.info("[%s] MusicBrainz tags found: %s", self._source, list(mb_tags.keys()))
                    else:
                        logger.warning("[%s] MusicBrainz returned no tags (ISRC: %r)", self._source, _isrc_for_mb)
                except concurrent.futures.TimeoutError:
                    logger.warning("[%s] MusicBrainz timed out after 12s, skipping MB tags", self._source)
                except Exception as exc:
                    logger.warning("[%s] MusicBrainz error: %s", self._source, exc)

            if extra.get("pic_id") and not metadata.cover_url:
                cover_url = await self._get_pic_url_async(extra.get("pic_id"))
                if cover_url:
                    metadata = metadata.model_copy(update={"cover_url": cover_url})

            opts = EmbedOptions(first_artist_only=kwargs.get("first_artist_only", False), cover_url=metadata.cover_url, extra_tags=mb_tags, embed_lyrics=kwargs.get("embed_lyrics", False), lyrics_providers=kwargs.get("lyrics_providers", []), enrich=kwargs.get("enrich_metadata", False), enrich_providers=kwargs.get("enrich_providers", []), enrich_qobuz_token=kwargs.get("qobuz_token", ""), is_album=kwargs.get("is_album", False))
            await embed_metadata_async(str(dest), metadata, opts)

            if gd_lyrics and gd_lyrics.strip():
                try:
                    from mutagen.flac import FLAC as _FLAC
                    audio = _FLAC(str(dest))
                    if "LYRICS" not in audio:
                        audio["LYRICS"] = gd_lyrics
                        audio.save()
                except Exception:
                    pass

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[%s] %s", self._source, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] unexpected error", self._source)
            return DownloadResult.fail(self.name, str(exc))


# Thin subclasses for specific GDStudio sources kept here to centralize logic
class JooxProvider(GDStudioProvider):
    name = "joox"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(source="joox", timeout_s=timeout_s)


class NeteaseProvider(GDStudioProvider):
    name = "netease"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(source="netease", timeout_s=timeout_s)


class MiguProvider(GDStudioProvider):
    name = "migu"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(source="migu", timeout_s=timeout_s)


class KuwoProvider(GDStudioProvider):
    name = "kuwo"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(source="kuwo", timeout_s=timeout_s)
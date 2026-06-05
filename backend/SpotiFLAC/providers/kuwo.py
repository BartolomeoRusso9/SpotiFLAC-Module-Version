from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .base import BaseProvider
from ..core.console import print_source_banner
from ..core.http import NetworkManager
from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError, ErrorKind, TrackNotFoundError
from ..core.tagger import embed_metadata, EmbedOptions
from ..core.download_validation import validate_downloaded_track
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_API_BASE = "https://music-api.gdstudio.xyz/api.php"
_SOURCE   = "kuwo"

# br=740 → 16-bit FLAC lossless, br=999 → 24-bit FLAC lossless.
_BR_LOSSLESS    = 999
_BR_LOSSLESS_CD = 740


class KuwoProvider(BaseProvider):
    """
    Provider for GD Studio Music API (music-api.gdstudio.xyz) — Kuwo source.

    Kuwo Music (酷我音乐) is a major Chinese music streaming platform with a
    large catalogue that includes lossless FLAC streams for many tracks.
    Strictly handles Lossless and Hi-Res audio (FLAC). MP3 is not supported.

    API credit: GD音乐台 (music.gdstudio.xyz) — CC BY-NC 4.0, study use only.
    """

    name = "kuwo"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = NetworkManager.get_sync_client()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})

    # ------------------------------------------------------------------
    # Internal API helpers
    # ------------------------------------------------------------------

    def _search(self, query: str, count: int = 10) -> list[dict]:
        """Search for tracks on Kuwo. Returns raw API result items."""
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "search",
                    "source": _SOURCE,
                    "name":   query,
                    "count":  count,
                    "pages":  1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("data", data.get("result", []))
        except Exception as exc:
            logger.debug("[kuwo] Search failed for '%s': %s", query, exc)
        return []

    def _get_stream(self, track_id: str) -> tuple[str, int]:
        """
        Request a lossless stream URL (br=999) from Kuwo via GD Studio API.
        Returns (url, actual_br).
        Rejects the stream if the API returns a lossy bitrate (< 740).
        """
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "url",
                    "source": _SOURCE,
                    "id":     track_id,
                    "br":     _BR_LOSSLESS,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data      = resp.json()
            url       = data.get("url", "")
            actual_br = int(data.get("br", 0))
            if not url:
                logger.warning("[kuwo] empty url for id=%s", track_id)
                return "", 0
            
            # Reject any lossy stream silently delivered by the API
            if actual_br < _BR_LOSSLESS_CD:
                logger.debug(
                    "[kuwo] Track %s returned lossy br=%d — rejected",
                    track_id, actual_br,
                )
                return "", actual_br
            
            return url, actual_br
        except Exception as exc:
            logger.debug("[kuwo] Stream fetch failed for id=%s: %s", track_id, exc)
        return "", 0

    def _get_pic_url(self, pic_id: str, size: int = 500) -> str:
        """Fetch album art URL from pic_id."""
        if not pic_id:
            return ""
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "pic",
                    "source": _SOURCE,
                    "id":     pic_id,
                    "size":   size,
                },
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json().get("url", "")
        except Exception as exc:
            logger.debug("[kuwo] Pic fetch failed for pic_id=%s: %s", pic_id, exc)
        return ""

    def _get_lyric(self, lyric_id: str) -> str:
        """
        Fetch LRC lyrics from Kuwo.
        Returns original-language LRC; falls back to Chinese translation if absent.
        """
        if not lyric_id:
            return ""
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "lyric",
                    "source": _SOURCE,
                    "id":     lyric_id,
                },
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("lyric", "") or data.get("tlyric", "")
        except Exception as exc:
            logger.debug("[kuwo] Lyric fetch failed for id=%s: %s", lyric_id, exc)
        return ""

    def _get_album_tracks(self, album_id: str) -> list[dict]:
        """
        Fetch the track list of a Kuwo album using the kuwo_album source suffix.
        """
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "search",
                    "source": f"{_SOURCE}_album",
                    "name":   album_id,
                    "count":  100,
                    "pages":  1,
                },
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.debug("[kuwo] Album tracks fetch failed for id=%s: %s", album_id, exc)
        return []

    # ------------------------------------------------------------------
    # Conversion helper
    # ------------------------------------------------------------------

    def _item_to_metadata(self, item: dict, position: int = 1) -> TrackMetadata:
        """Convert a GD Studio search result item into a TrackMetadata."""
        track_id = str(item.get("id", ""))
        title    = item.get("name", "Unknown")

        raw_artists = item.get("artist", [])
        if isinstance(raw_artists, list):
            artist_str = ", ".join(
                a.get("name", "") if isinstance(a, dict) else str(a)
                for a in raw_artists
            ).strip(", ") or "Unknown"
        else:
            artist_str = str(raw_artists) or "Unknown"

        album  = item.get("album", "Unknown")
        pic_id = str(item.get("pic_id", ""))

        cover_url = self._get_pic_url(pic_id) if pic_id else ""

        return TrackMetadata(
            id           = f"kuwo_{track_id}",
            title        = title,
            artists      = artist_str,
            album        = album,
            album_artist = artist_str,
            duration_ms  = 0,
            cover_url    = cover_url,
            external_url = "",
            extra_info   = {
                "provider":     "kuwo",
                "raw_track_id": track_id,
                "pic_id":       pic_id,
                "lyric_id":     str(item.get("lyric_id", track_id)),
            },
        )

    # ------------------------------------------------------------------
    # get_url  (collection fetching)
    # ------------------------------------------------------------------

    def get_url(self, url: str) -> tuple[str, list[TrackMetadata]]:
        """
        Accepts:
          • A numeric Kuwo track/album ID embedded in a URL or bare string
          • A plain search query (title, artist, …)

        Returns (collection_name, [TrackMetadata]).
        """
        match = re.search(r"(\d{5,})", url)

        if match and "_album" in url.lower():
            album_id = match.group(1)
            items = self._get_album_tracks(album_id)
            if items:
                tracks = [self._item_to_metadata(it, i + 1) for i, it in enumerate(items)]
                album_name = tracks[0].album if tracks else "Unknown Album"
                return album_name, tracks

        if match:
            track_id = match.group(1)
            items = self._search(track_id, count=1)
            if items:
                meta = self._item_to_metadata(items[0])
                return meta.title, [meta]

        query = url.strip()
        items = self._search(query, count=20)
        if not items:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                f"No results for: {query}",
                self.name,
            )
        tracks = [self._item_to_metadata(it, i + 1) for i, it in enumerate(items)]
        return f"Search: {query}", tracks

    # ------------------------------------------------------------------
    # download_track  (BaseProvider interface)
    # ------------------------------------------------------------------

    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            quality:             str              = "LOSSLESS",
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

        try:
            # ── 1. Resolve raw Kuwo track ID ──────────────────────────────
            extra        = metadata.extra_info or {}
            raw_track_id = extra.get("raw_track_id", "")

            if not raw_track_id:
                query = f"{metadata.title} {metadata.first_artist}".strip()
                logger.info("[kuwo] Searching for: %s", query)
                items = self._search(query, count=5)
                if not items:
                    raise TrackNotFoundError(self.name, f"Track not found on Kuwo: {query}")
                raw_track_id = str(items[0].get("id", ""))
                if not raw_track_id:
                    raise TrackNotFoundError(self.name, "Empty track ID from Kuwo search")
                extra = {
                    "raw_track_id": raw_track_id,
                    "pic_id":       str(items[0].get("pic_id", "")),
                    "lyric_id":     str(items[0].get("lyric_id", raw_track_id)),
                }

            # ── 2. Fetch stream URL ───────────────────────────────────────
            dl_url, actual_br = self._get_stream(raw_track_id)
            if not dl_url:
                raise SpotiflacError(
                    ErrorKind.UNAVAILABLE,
                    f"No lossless stream available on Kuwo for id={raw_track_id}",
                    self.name,
                )

            # ── 3. Determine file extension ───────────────────────────────
            extension = ".flac"
            quality_label = f"FLAC {actual_br}kbps"

            # ── 4. Cover art ──────────────────────────────────────────────
            cover_url = metadata.cover_url
            if not cover_url:
                pic_id = extra.get("pic_id", "")
                if pic_id:
                    cover_url = self._get_pic_url(pic_id)

            # ── 5. Build destination path ─────────────────────────────────
            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
                extension=extension,
            )

            if self._file_exists(dest):
                return DownloadResult.skipped_result(self.name, str(dest), fmt="flac")

            # ── 6. MusicBrainz async fetch ────────────────────────────────
            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None

            # ── 7. Source banner ──────────────────────────────────────────
            print_source_banner("kuwo", _API_BASE, quality_label)

            # ── 8. Download ───────────────────────────────────────────────
            logger.info("[kuwo] Downloading '%s' (id=%s, br=%d)", metadata.title, raw_track_id, actual_br)
            self._http.stream_to_file(dl_url, str(dest), self._progress_cb)

            # ── 9. Validate (preview / duration mismatch) ─────────────────
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                if dest.exists():
                    os.remove(str(dest))
                return DownloadResult.fail(self.name, f"Validation failed: {err_msg}")

            # ── 10. Lyrics from Kuwo ──────────────────────────────────────
            lyric_id  = extra.get("lyric_id", raw_track_id)
            gd_lyrics: str | None = None
            if embed_lyrics and lyric_id:
                gd_lyrics = self._get_lyric(lyric_id) or None

            # ── 11. MusicBrainz tags ──────────────────────────────────────
            mb_tags: dict[str, str] = {}
            if mb_fetcher:
                res     = mb_fetcher.future.result()
                mb_tags = mb_result_to_tags(res)

            # ── 12. Embed metadata ────────────────────────────────────────
            if cover_url and cover_url != metadata.cover_url:
                metadata = metadata.model_copy(update={"cover_url": cover_url})

            opts = EmbedOptions(
                first_artist_only  = first_artist_only,
                cover_url          = cover_url or metadata.cover_url,
                extra_tags         = mb_tags,
                embed_lyrics       = embed_lyrics,
                lyrics_providers   = lyrics_providers or [],
                enrich             = enrich_metadata,
                enrich_providers   = enrich_providers,
                enrich_qobuz_token = qobuz_token or "",
                is_album           = is_album,
            )
            embed_metadata(str(dest), metadata, opts, session=self._session)

            # Inject Kuwo lyrics only if the normal providers found nothing
            if gd_lyrics and gd_lyrics.strip():
                try:
                    from mutagen.flac import FLAC as _FLAC
                    audio = _FLAC(str(dest))
                    if "LYRICS" not in audio:
                        audio["LYRICS"] = gd_lyrics
                        audio.save()
                        logger.debug("[kuwo] Kuwo lyrics embedded (%d chars)", len(gd_lyrics))
                except Exception as exc:
                    logger.warning("[kuwo] Lyrics embed failed: %s", exc)

            return DownloadResult.ok(self.name, str(dest), fmt="flac")

        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] Unexpected error", self.name)
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
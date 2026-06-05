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
_API_BASE_WJHE = "https://music.wjhe.top/api/music/joox"
_SOURCE   = "joox"

# ---------------------------------------------------------------------------
# GD Studio Bitrate (br) mappings
# ---------------------------------------------------------------------------
_BR_HIRES       = 999  # 24-bit FLAC
_BR_LOSSLESS_CD = 740  # 16-bit FLAC

class JooxProvider(BaseProvider):
    """
    Provider for JOOX using WJHE and GD Studio API endpoints.
    Strictly handles Lossless and Hi-Res audio (FLAC, M4A). MP3 is not supported.
    """

    name = "joox"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = NetworkManager.get_sync_client()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})

    # ------------------------------------------------------------------
    # Internal API helpers
    # ------------------------------------------------------------------

    def _search(self, query: str, count: int = 10) -> list[dict]:
        """Search for tracks on JOOX via GD Studio."""
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
            logger.debug("[joox] Search failed for '%s': %s", query, exc)
        return []

    def _get_stream_gdstudio(self, track_id: str, br: int = _BR_LOSSLESS_CD) -> tuple[str, int]:
        """
        Request a stream URL from GD Studio using the legacy `br` parameter.
        """
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "url",
                    "source": _SOURCE,
                    "id":     track_id,
                    "br":     br,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data      = resp.json()
            url       = data.get("url", "")
            actual_br = int(data.get("br", 0))

            if not url or actual_br < _BR_LOSSLESS_CD:
                logger.debug(
                    "[joox-gd] Empty or lossy stream rejected for id=%s br=%d (actual=%d)",
                    track_id, br, actual_br
                )
                return "", actual_br

            return url, actual_br
        except Exception as exc:
            logger.debug("[joox-gd] Stream fetch failed for id=%s: %s", track_id, exc)
        return "", 0

    def _get_stream_wjhe(self, track_id: str, quality: int = 1000, fmt: str = "flac") -> str:
        url = f"{_API_BASE_WJHE}/url"
        params = {
            "ID":      track_id,
            "quality": quality,
            "format":  fmt,
        }
        
        try:
            resp = self._session.get(url, params=params, stream=True, timeout=10)
            
            if resp.status_code == 200:
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" not in content_type.lower():
                    final_url = str(resp.url)
                    resp.close()
                    return final_url
            
            resp.close()
        except Exception as exc:
            logger.debug(
                "[joox-wjhe] Stream fetch failed for id=%s (quality=%d fmt=%s): %s",
                track_id, quality, fmt, exc
            )
        return ""

    def _get_stream_with_fallback(
        self,
        track_id: str,
        requested_quality: str,
    ) -> tuple[str, str, int, str]:
        """
        Try to obtain a stream URL, combining WJHE and GD Studio.
        """
        q = (requested_quality or "LOSSLESS").upper()

        # Define fallback chains. Tuples represent:
        # (InternalType, WJHE_Quality, WJHE_Format, GD_br, UI_Label)
        if q in ("HIRES", "HIGHEST", "BEST", "MAX", "LOSSLESS_HIRES", "HIRES_LOSSLESS"):
            attempts = [
                ("FLAC24",  3000, "flac", _BR_HIRES,       "FLAC 24bit"),
                ("FLAC16",  1000, "flac", _BR_LOSSLESS_CD, "FLAC 16bit"),
                ("M4A24",   3000, "m4a",  None,            "M4A 24bit"),
                ("M4A16",   1000, "m4a",  None,            "M4A 16bit"),
            ]
        else:
            attempts = [
                ("FLAC16",  1000, "flac", _BR_LOSSLESS_CD, "FLAC 16bit"),
                ("M4A16",   1000, "m4a",  None,            "M4A 16bit"),
            ]

        for fmt_type, wjhe_q, wjhe_f, gd_br, label in attempts:
            ext = ".flac" if "FLAC" in fmt_type else ".m4a"

            # 1. Try WJHE
            if wjhe_q and wjhe_f:
                url_wjhe = self._get_stream_wjhe(track_id, quality=wjhe_q, fmt=wjhe_f)
                if url_wjhe:
                    quality_label = f"{label} (WJHE)"
                    logger.info(
                        "[joox] Stream obtained (WJHE): quality=%d fmt=%s ext=%s",
                        wjhe_q, wjhe_f, ext,
                    )
                    return url_wjhe, ext, 0, quality_label

            # 2. Try GD Studio
            if gd_br:
                url_gd, actual_br = self._get_stream_gdstudio(track_id, br=gd_br)
                if url_gd:
                    # Prevent GD Studio from silently returning lossy
                    if actual_br < _BR_LOSSLESS_CD:
                        continue

                    final_ext = ".flac"
                    quality_label = f"{label} (GD {actual_br}kbps)"
                    logger.info(
                        "[joox] Stream obtained (GD): requested_br=%d actual_br=%d ext=%s",
                        gd_br, actual_br, final_ext,
                    )
                    return url_gd, final_ext, actual_br, quality_label

        return "", "", 0, ""

    def _get_pic_url(self, pic_id: str, size: int = 500) -> str:
        """Fetch album art URL from pic_id via GD Studio."""
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
            logger.debug("[joox] Pic fetch failed for pic_id=%s: %s", pic_id, exc)
        return ""

    def _get_lyric(self, lyric_id: str) -> str:
        """Fetch LRC lyrics via GD Studio."""
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
            logger.debug("[joox] Lyric fetch failed for id=%s: %s", lyric_id, exc)
        return ""

    def _get_album_tracks(self, album_id: str) -> list[dict]:
        """
        Fetch the track list of a JOOX album via GD Studio.
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
            logger.debug("[joox] Album tracks fetch failed for id=%s: %s", album_id, exc)
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
            id           = f"joox_{track_id}",
            title        = title,
            artists      = artist_str,
            album        = album,
            album_artist = artist_str,
            duration_ms  = 0,
            cover_url    = cover_url,
            external_url = "",
            extra_info   = {
                "provider":     "joox",
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
        Accepts a numeric JOOX track/album ID or a plain search query.
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
            # ── 1. Resolve raw JOOX track ID ──────────────────────────────
            extra        = metadata.extra_info or {}
            raw_track_id = extra.get("raw_track_id", "")

            if not raw_track_id:
                query = f"{metadata.title} {metadata.first_artist}".strip()
                logger.info("[joox] Searching for: %s", query)
                items = self._search(query, count=5)
                if not items:
                    raise TrackNotFoundError(self.name, f"Track not found on JOOX: {query}")
                raw_track_id = str(items[0].get("id", ""))
                if not raw_track_id:
                    raise TrackNotFoundError(self.name, "Empty track ID from JOOX search")
                extra = {
                    "raw_track_id": raw_track_id,
                    "pic_id":       str(items[0].get("pic_id", "")),
                    "lyric_id":     str(items[0].get("lyric_id", raw_track_id)),
                }

            # ── 2. Fetch stream URL with quality-aware fallback chain ──────
            dl_url, extension, actual_br, quality_label = self._get_stream_with_fallback(
                raw_track_id, quality
            )

            if not dl_url:
                raise SpotiflacError(
                    ErrorKind.UNAVAILABLE,
                    f"No stream available on JOOX for id={raw_track_id} (quality={quality})",
                    self.name,
                )

            # ── 3. Cover art ──────────────────────────────────────────────
            cover_url = metadata.cover_url
            if not cover_url:
                pic_id = extra.get("pic_id", "")
                if pic_id:
                    cover_url = self._get_pic_url(pic_id)

            # ── 4. Build destination path ─────────────────────────────────
            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
                extension=extension,
            )

            if self._file_exists(dest):
                fmt = extension.lstrip(".")
                return DownloadResult.skipped_result(self.name, str(dest), fmt=fmt)

            # ── 5. MusicBrainz async fetch ────────────────────────────────
            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None

            # ── 6. Source banner ──────────────────────────────────────────
            print_source_banner("joox", _API_BASE, quality_label)

            # ── 7. Download ───────────────────────────────────────────────
            logger.info(
                "[joox] Downloading '%s' (id=%s, ext=%s)",
                metadata.title, raw_track_id, extension
            )
            self._http.stream_to_file(dl_url, str(dest), self._progress_cb)

            # ── 8. Validate (preview / duration mismatch) ─────────────────
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                if dest.exists():
                    os.remove(str(dest))
                return DownloadResult.fail(self.name, f"Validation failed: {err_msg}")

            # ── 9. Lyrics from JOOX ───────────────────────────────────────
            lyric_id  = extra.get("lyric_id", raw_track_id)
            gd_lyrics: str | None = None
            if embed_lyrics and lyric_id:
                gd_lyrics = self._get_lyric(lyric_id) or None

            # ── 10. MusicBrainz tags ──────────────────────────────────────
            mb_tags: dict[str, str] = {}
            if mb_fetcher:
                res     = mb_fetcher.future.result()
                mb_tags = mb_result_to_tags(res)

            # ── 11. Embed metadata ────────────────────────────────────────
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

            # Inject JOOX lyrics if the normal providers found nothing
            if gd_lyrics and gd_lyrics.strip():
                try:
                    if extension == ".flac":
                        from mutagen.flac import FLAC as _FLAC
                        audio = _FLAC(str(dest))
                        if "LYRICS" not in audio:
                            audio["LYRICS"] = gd_lyrics
                            audio.save()
                    elif extension == ".m4a":
                        from mutagen.mp4 import MP4
                        audio = MP4(str(dest))
                        if "\xa9lyr" not in audio:
                            audio["\xa9lyr"] = [gd_lyrics]
                            audio.save()
                    logger.debug("[joox] JOOX lyrics embedded (%d chars)", len(gd_lyrics))
                except Exception as exc:
                    logger.warning("[joox] Lyrics embed failed: %s", exc)

            fmt = extension.lstrip(".")
            return DownloadResult.ok(self.name, str(dest), fmt=fmt)

        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] Unexpected error", self.name)
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
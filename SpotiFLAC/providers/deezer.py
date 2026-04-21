# deezer_provider.py
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests
from mutagen.flac import FLAC

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_MAX_RETRIES   = 2
_RETRY_DELAY_S = 0.5
_API_TIMEOUT_S = 25

# Cache (porta deezerCacheTTL / deezerCacheCleanupInterval dal Go)
_CACHE_TTL_S             = 10 * 60   # 10 minuti
_CACHE_CLEANUP_INTERVAL_S = 5 * 60   # 5 minuti
_MAX_TRACK_CACHE         = 4000
_MAX_SEARCH_CACHE        = 300

# Errori retryable (porta isRetryable da getJSON nel Go)
_RETRYABLE_SUBSTRINGS = (
    "timeout", "connection reset", "connection refused", "EOF",
    "status 5", "status 429", "RemoteDisconnected",
)


# ---------------------------------------------------------------------------
# Cache entry (porta cacheEntry dal Go)
# ---------------------------------------------------------------------------

class _CacheEntry:
    __slots__ = ("data", "expires_at")

    def __init__(self, data: Any, ttl_s: float = _CACHE_TTL_S) -> None:
        self.data       = data
        self.expires_at = time.monotonic() + ttl_s

    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


# ---------------------------------------------------------------------------
# DeezerProvider
# ---------------------------------------------------------------------------

class DeezerProvider(BaseProvider):
    name = "deezer"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})

        # Cache (porta searchCache / isrcCache / cacheMu / lastCacheCleanup dal Go)
        self._track_cache:  dict[str, _CacheEntry] = {}   # isrc  → track_data dict
        self._search_cache: dict[str, _CacheEntry] = {}   # url   → json dict
        self._cache_mu              = threading.Lock()
        self._last_cache_cleanup    = 0.0

    # ------------------------------------------------------------------
    # Cache helpers (porta maybeCleanupCachesLocked / trimStringCache dal Go)
    # ------------------------------------------------------------------

    def _maybe_cleanup_cache(self) -> None:
        """Rimuove entry scadute periodicamente. Chiamare con _cache_mu acquisito."""
        now = time.monotonic()
        if now - self._last_cache_cleanup < _CACHE_CLEANUP_INTERVAL_S:
            return
        self._last_cache_cleanup = now

        for cache in (self._track_cache, self._search_cache):
            expired = [k for k, v in cache.items() if v.is_expired()]
            for k in expired:
                del cache[k]

        # Trim per dimensione massima (porta trimStringCacheEntriesLocked dal Go)
        self._trim_cache(self._track_cache,  _MAX_TRACK_CACHE)
        self._trim_cache(self._search_cache, _MAX_SEARCH_CACHE)

    @staticmethod
    def _trim_cache(cache: dict, max_entries: int) -> None:
        if len(cache) <= max_entries:
            return
        # Rimuove le entry con expires_at più basso (le più vecchie)
        sorted_keys = sorted(cache, key=lambda k: cache[k].expires_at)
        for k in sorted_keys[:len(cache) - max_entries]:
            del cache[k]

    # ------------------------------------------------------------------
    # HTTP con retry (porta getJSON + isRetryable dal Go)
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict:
        last_err: Exception | None = None
        delay = _RETRY_DELAY_S

        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                logger.debug("[deezer] retry %d/%d after %.1fs: %s", attempt, _MAX_RETRIES, delay, url)
                time.sleep(delay)
                delay *= 2
            try:
                resp = self._session.get(url, timeout=_API_TIMEOUT_S)

                if resp.status_code == 429:
                    delay    = max(delay, 2.0)
                    last_err = RuntimeError("rate limited (429)")
                    continue
                if resp.status_code >= 500:
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    # Retryable: continua
                    continue

                resp.raise_for_status()
                return resp.json()

            except (requests.Timeout, requests.ConnectionError) as exc:
                # Sempre retryable
                last_err = exc
                continue
            except Exception as exc:
                err_str = str(exc)
                # Porta isRetryable dal Go: controlla substring
                if any(s in err_str for s in _RETRYABLE_SUBSTRINGS):
                    last_err = exc
                    continue
                # Non retryable: rilancia subito
                raise RuntimeError(f"Deezer request failed: {exc}") from exc

        raise RuntimeError(f"All {_MAX_RETRIES + 1} attempts failed: {last_err}")

    def _get_json_cached(self, url: str) -> dict:
        """Wrapper con cache in-memory (porta searchCache dal Go)."""
        with self._cache_mu:
            entry = self._search_cache.get(url)
            if entry and not entry.is_expired():
                return entry.data
            self._maybe_cleanup_cache()

        data = self._get_json(url)

        with self._cache_mu:
            self._search_cache[url] = _CacheEntry(data)
            self._maybe_cleanup_cache()

        return data

    # ------------------------------------------------------------------
    # API Deezer
    # ------------------------------------------------------------------

    def _get_track_by_isrc(self, isrc: str) -> dict | None:
        # Porta isrcCache dal Go: controlla cache prima della chiamata HTTP
        with self._cache_mu:
            entry = self._track_cache.get(isrc)
            if entry and not entry.is_expired():
                logger.debug("[deezer] ISRC cache hit: %s", isrc)
                return entry.data

        try:
            data = self._get_json(f"https://api.deezer.com/2.0/track/isrc:{isrc}")
            if "error" in data:
                logger.warning("[deezer] API error: %s", data["error"].get("message", "?"))
                return None

            with self._cache_mu:
                self._track_cache[isrc] = _CacheEntry(data)
                self._maybe_cleanup_cache()

            return data
        except Exception as exc:
            logger.warning("[deezer] get_track_by_isrc failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _best_cover(album: dict) -> str:
        return (
                album.get("cover_xl")
                or album.get("cover_big")
                or album.get("cover_medium")
                or album.get("cover")
                or ""
        )

    @staticmethod
    def _track_artist_display(track_data: dict) -> str:
        contributors = track_data.get("contributors", [])
        if contributors:
            return ", ".join(c["name"] for c in contributors if c.get("name"))
        return track_data.get("artist", {}).get("name", "")

    def _extract_metadata(self, track_data: dict) -> dict:
        album = track_data.get("album", {})
        return {
            "title":          track_data.get("title", ""),
            "track_position": track_data.get("track_position", 1),
            "disk_number":    track_data.get("disk_number", 1),
            "isrc":           track_data.get("isrc", ""),
            "release_date":   track_data.get("release_date", ""),
            "artist":         track_data.get("artist", {}).get("name", ""),
            "artists":        self._track_artist_display(track_data),
            "album":          album.get("title", ""),
            "cover_url":      self._best_cover(album),
        }

    # ------------------------------------------------------------------
    # Cover art
    # ------------------------------------------------------------------

    def _download_cover(self, cover_url: str, dest_base: str) -> str | None:
        if not cover_url:
            return None
        try:
            resp = self._session.get(cover_url, timeout=15)
            resp.raise_for_status()
            path = f"{dest_base}_cover.jpg"
            with open(path, "wb") as f:
                f.write(resp.content)
            return path
        except Exception as exc:
            logger.warning("[deezer] Cover download failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Tag embedding
    # ------------------------------------------------------------------

    def _embed_metadata(self, file_path: str, meta: dict, cover_path: str | None) -> None:
        try:
            audio = FLAC(file_path)
            audio.clear()

            for tag, value in {
                "TITLE":       meta.get("title"),
                "ARTIST":      meta.get("artists") or meta.get("artist"),
                "ALBUM":       meta.get("album"),
                "DATE":        meta.get("release_date"),
                "TRACKNUMBER": str(meta["track_position"]) if meta.get("track_position") else None,
                "DISCNUMBER":  str(meta["disk_number"])    if meta.get("disk_number")    else None,
                "ISRC":        meta.get("isrc"),
            }.items():
                if value:
                    audio[tag] = value

            if cover_path and os.path.exists(cover_path):
                from mutagen.flac import Picture
                pic      = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                with open(cover_path, "rb") as f:
                    pic.data = f.read()
                audio.add_picture(pic)

            audio.save()
        except Exception as exc:
            logger.warning("[deezer] embed_metadata failed: %s", exc)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    @staticmethod
    def _safe(s: str) -> str:
        return "".join(c for c in s if c.isalnum() or c in " -_").strip()

    def _download_flac(self, isrc: str, output_dir: str) -> str | None:
        track_data = self._get_track_by_isrc(isrc)
        if not track_data:
            return None

        meta     = self._extract_metadata(track_data)
        track_id = track_data.get("id")
        if not track_id:
            logger.warning("[deezer] No track ID in response")
            return None

        logger.info("[deezer] Found: %s - %s", meta["artists"], meta["title"])

        try:
            # Anche questo è cacheable: stessa traccia richiesta due volte
            # nella stessa sessione non rifà la chiamata HTTP
            api_data = self._get_json_cached(f"https://api.deezmate.com/dl/{track_id}")
            if not api_data.get("success"):
                logger.warning("[deezer] deezmate returned success=false")
                return None
            flac_url = api_data.get("links", {}).get("flac")
            if not flac_url:
                logger.warning("[deezer] No FLAC URL in deezmate response")
                return None
        except Exception as exc:
            logger.warning("[deezer] Failed to get download URL: %s", exc)
            return None

        filename  = f"{self._safe(meta['artists'])} - {self._safe(meta['title'])}.flac"
        file_path = os.path.join(output_dir, filename)

        try:
            os.makedirs(output_dir, exist_ok=True)
            with self._session.get(flac_url, stream=True, timeout=_API_TIMEOUT_S) as resp:
                resp.raise_for_status()
                total    = int(resp.headers.get("content-length", 0))
                received = 0
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            received += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(received, total)
            logger.info("[deezer] Downloaded %.2f MB", received / (1024 * 1024))
        except Exception as exc:
            logger.warning("[deezer] Download failed: %s", exc)
            if os.path.exists(file_path):
                os.remove(file_path)
            return None

        base       = os.path.join(output_dir, f"{self._safe(meta['artists'])} - {self._safe(meta['title'])}")
        cover_path = self._download_cover(meta.get("cover_url", ""), base)
        self._embed_metadata(file_path, meta, cover_path)
        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)

        return file_path

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot(directory: str) -> set[str]:
        result = set()
        for root, _, files in os.walk(directory):
            for f in files:
                if f.lower().endswith(".flac"):
                    result.add(os.path.join(root, f))
        return result

    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            filename_format:     str  = "{title} - {artist}",
            position:            int  = 1,
            include_track_num:   bool = False,
            use_album_track_num: bool = False,
            first_artist_only:   bool = False,
            allow_fallback:      bool = True,
            **kwargs,
    ) -> DownloadResult:
        if not metadata.isrc:
            return DownloadResult.fail(self.name, "No ISRC available for Deezer")

        try:
            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            before     = self._snapshot(output_dir)
            downloaded = self._download_flac(metadata.isrc, output_dir)

            if not downloaded:
                new_files = self._snapshot(output_dir) - before
                if not new_files:
                    return DownloadResult.fail(self.name, "No FLAC file downloaded")
                downloaded = max(new_files, key=os.path.getctime)

            if os.path.abspath(downloaded) != os.path.abspath(str(dest)):
                import shutil
                os.makedirs(os.path.dirname(str(dest)), exist_ok=True)
                shutil.move(downloaded, str(dest))

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[deezer] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[deezer] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

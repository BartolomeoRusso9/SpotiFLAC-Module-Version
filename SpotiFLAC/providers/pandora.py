"""
PandoraProvider — porta completa di index.js in Python.

Supporta:
  - URL Pandora web (www.pandora.com/artist/...)
  - App link Pandora (pandora.app.link/...)
  - Pandora ID diretti (TR:..., AL:...)
  - Risoluzione via Song.link + arricchimento Deezer
  - Download via api.zarz.moe/v1/dl/pan
  - Qualità: mp3_192 (default), aac_64, aac_32
  - Pipeline completa: MusicBrainz, lyrics, enrichment, tagging
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

import httpx

from ..core.console import print_source_banner
from ..core.download_validation import validate_downloaded_track_async
from ..core.endpoints import get_pandora_base_and_path
from ..core.errors import ErrorKind, SpotiflacError, TrackNotFoundError
from ..core.http import RetryConfig, async_songlink_rate_limiter
from ..core.models import DownloadResult, TrackMetadata
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags
from ..core.quality import normalize_quality
from ..core.tagger import EmbedOptions, _print_mb_summary, embed_metadata_async
from .base import BaseProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Compiled Regexes
# ---------------------------------------------------------------------------

_SONGLINK_URL    = "https://api.song.link/v1-alpha.1/links"
_DEEZER_API_URL  = "https://api.deezer.com"
_PANDORA_BASE    = "https://www.pandora.com"
_USER_COUNTRY    = "US"

_MOBILE_UA = "SpotiFLAC-Mobile/4.5.0"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_QUALITY_MAP = {
    "mp3_192": "highQuality",
    "aac_64":  "mediumQuality",
    "aac_32":  "lowQuality",
}

# Pre-compiled Regex Patterns for performance
_PANDORA_ID_RE = re.compile(r'\b(TR|AL|AR|PL|ST):([A-Za-z0-9]+)\b', re.IGNORECASE)
_PANDORA_PRETTY_RE = re.compile(r'(?:^|[/?=&])((TR|AL|AR|PL|ST)(\d[A-Za-z0-9]*))(?=[/?&#]|$)', re.IGNORECASE)
_HTTP_RE = re.compile(r'^http://', re.IGNORECASE)
_STRIP_QUERY_RE = re.compile(r'[?#].*$')
_DASH_UNDERSCORE_RE = re.compile(r'[-_]+')
_WHITESPACE_RE = re.compile(r'\s+')
_FALLBACK_URL_RE = re.compile(r'^https?://([^/]+)(/[^?#]*)?', re.IGNORECASE)
_PANDORA_LINK_RE = re.compile(r'https?://(?:www\.)?pandora\.com/[^"\'<>\\\s]+', re.IGNORECASE)
_PANDORA_ID_PARAM_RE = re.compile(r'pandoraId=([^"\'&<>\s]+)', re.IGNORECASE)
_DEEZER_TRACK_URL_RE = re.compile(r'deezer\.com/(?:[a-z]{2}/)?track/(\d+)', re.IGNORECASE)

# ID Check Regexes
_TR_PREFIX_RE = re.compile(r'^TR(?::)?', re.IGNORECASE)
_AL_PREFIX_RE = re.compile(r'^AL(?::)?', re.IGNORECASE)
_DIRECT_ID_RE = re.compile(r'^(TR|AL|AR|PL|ST):?[A-Za-z0-9]+$', re.IGNORECASE)
_SPOTIFY_BARE_ID_RE = re.compile(r'^[A-Za-z0-9]{22}$')
_SPOTIFY_URI_RE = re.compile(r'^spotify:track:[A-Za-z0-9]{22}$', re.IGNORECASE)
_DEEZER_BARE_ID_RE = re.compile(r'^\d+$')
_EXT_MP3_RE = re.compile(r'\.mp3(?:$|\?)', re.IGNORECASE)
_EXT_M4A_RE = re.compile(r'\.m4a(?:$|\?)', re.IGNORECASE)
_EXT_MP4_RE = re.compile(r'\.mp4(?:$|\?)', re.IGNORECASE)

# ---------------------------------------------------------------------------
# URL / ID helpers
# ---------------------------------------------------------------------------

def _normalize_secure_url(url: str | None) -> str:
    url_str = (url or "").strip()
    return _HTTP_RE.sub('https://', url_str)


def _strip_url_query(url: str | None) -> str:
    return _STRIP_QUERY_RE.sub('', (url or "").strip())


def _title_case_from_slug(value: str | None) -> str:
    """Converte un slug URL in Title Case (es. 'rock-and-roll' → 'Rock And Roll')."""
    value = (value or "").strip()
    if not value:
        return ""
    cleaned = _DASH_UNDERSCORE_RE.sub(' ', value)
    cleaned = _WHITESPACE_RE.sub(' ', cleaned).strip()
    return cleaned.title()


def _try_parse_url(url: str | None) -> dict[str, str] | None:
    """Parsifica un URL e restituisce hostname e pathname."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            return {
                "hostname": parsed.hostname,
                "pathname": parsed.path or "/",
            }
    except ValueError:
        pass
        
    m = _FALLBACK_URL_RE.match(url)
    if not m:
        return None
    return {"hostname": m.group(1) or "", "pathname": m.group(2) or "/"}


def _normalize_pandora_id(value: str | None) -> str:
    """Estrae e normalizza un Pandora ID (TR:..., AL:...) dall'input."""
    raw = (value or "").strip()
    if not raw:
        return ""

    try:
        raw = unquote(raw)
    except Exception:
        pass

    m = _PANDORA_ID_RE.search(raw)
    if m:
        prefix = m.group(1).upper()
        id_part = m.group(2).upper()
        return f"{prefix}:{id_part}"

    pm = _PANDORA_PRETTY_RE.search(raw)
    if pm:
        prefix = pm.group(2).upper()
        id_part = pm.group(3).upper()
        return f"{prefix}:{id_part}"

    return ""


def _extract_pandora_track_id(value: str | None) -> str:
    pid = _normalize_pandora_id(value)
    return pid if _TR_PREFIX_RE.match(pid) else ""


def _extract_pandora_album_id(value: str | None) -> str:
    pid = _normalize_pandora_id(value)
    return pid if _AL_PREFIX_RE.match(pid) else ""


def _build_pandora_url(pandora_id: str) -> str:
    return f"{_PANDORA_BASE}/{pandora_id.strip()}"


def _normalize_pandora_web_url(url: str | None) -> str:
    normalized = _strip_url_query(_normalize_secure_url(url))
    parsed = _try_parse_url(normalized)
    if not parsed or "pandora.com" not in parsed["hostname"].lower():
        return normalized

    segments = [s for s in parsed["pathname"].strip("/").split("/") if s]
    if not segments:
        return normalized

    if segments[0] in ("artist", "playlist"):
        return normalized

    last = segments[-1]
    if (_TR_PREFIX_RE.match(last) and len(segments) >= 4) or \
       (_AL_PREFIX_RE.match(last) and len(segments) >= 3):
        return _PANDORA_BASE + "/artist/" + "/".join(segments)

    return normalized


def _normalize_pandora_canonical_url(input_url: str | None, pandora_id: str) -> str:
    normalized = _normalize_secure_url(input_url)

    if "pandora.com/" in normalized:
        if _extract_pandora_track_id(normalized) or _extract_pandora_album_id(normalized):
            return _normalize_pandora_web_url(normalized)

    return _build_pandora_url(pandora_id)


def _is_pandora_app_link(url: str | None) -> bool:
    parsed = _try_parse_url(url)
    if not parsed or not parsed["hostname"]:
        return False
    return parsed["hostname"].lower() == "pandora.app.link"


def _extract_pandora_url_from_html(html: str | None) -> str:
    body = html or ""
    if not body:
        return ""

    matches = _PANDORA_LINK_RE.findall(body)
    for candidate in matches:
        normalized = _normalize_secure_url(candidate).replace("&amp;", "&")
        if "pandora.com/" in normalized:
            return normalized

    m = _PANDORA_ID_PARAM_RE.search(body)
    if m:
        decoded = m.group(1)
        try:
            decoded = unquote(decoded)
        except Exception:
            pass
        pid = _normalize_pandora_id(decoded)
        if pid:
            return _build_pandora_url(pid)

    return ""


def _parse_pandora_pretty_url(url: str | None) -> dict[str, str] | None:
    if not url:
        return None
        
    parsed = _try_parse_url(_normalize_pandora_web_url(url))
    if not parsed or "pandora.com" not in parsed["hostname"].lower():
        return None

    segments = [s for s in parsed["pathname"].strip("/").split("/") if s]
    if not segments:
        return None

    if segments[0] == "artist":
        if len(segments) >= 5:
            return {
                "type":       "track",
                "artistName": _title_case_from_slug(segments[1]),
                "albumName":  _title_case_from_slug(segments[2]),
                "trackName":  _title_case_from_slug(segments[3]),
            }
        if len(segments) >= 4 and _AL_PREFIX_RE.match(segments[3]):
            return {
                "type":       "album",
                "artistName": _title_case_from_slug(segments[1]),
                "albumName":  _title_case_from_slug(segments[2]),
            }
        if len(segments) >= 2:
            return {
                "type":       "artist",
                "artistName": _title_case_from_slug(segments[1]),
            }

    if segments[0] == "playlist":
        return {
            "type":         "playlist",
            "playlistName": _title_case_from_slug(segments[1] if len(segments) > 1 else "Pandora Playlist"),
        }

    return None


def _select_quality_link(cdn_links: dict[str, Any], quality: str) -> dict[str, Any] | None:
    requested = (quality or "mp3_192").lower()

    if requested == "aac_64" and cdn_links.get("mediumQuality"):
        return cdn_links["mediumQuality"]
    if requested == "aac_32" and cdn_links.get("lowQuality"):
        return cdn_links["lowQuality"]
        
    return (
        cdn_links.get("highQuality") or 
        cdn_links.get("mediumQuality") or 
        cdn_links.get("lowQuality")
    )


def _output_extension_for_link(link_info: dict[str, Any] | None) -> str:
    if not link_info:
        return ".bin"

    encoding = str(link_info.get("encoding", "")).lower()
    if encoding in ("mp3", "mpeg"):
        return ".mp3"
    if "aac" in encoding:
        return ".m4a"

    url = str(link_info.get("url", ""))
    if _EXT_MP3_RE.search(url):
        return ".mp3"
    if _EXT_M4A_RE.search(url) or _EXT_MP4_RE.search(url):
        return ".m4a"

    return ".bin"


def _ua_for_url(url: str | None) -> str:
    text = (url or "").strip().lower()
    if text.startswith(("https://api.zarz.moe", "http://api.zarz.moe")):
        return _MOBILE_UA
    return _DEFAULT_UA


def _is_pandora_url(url: str | None) -> bool:
    val = (url or "").lower()
    return "pandora.com" in val or "pandora.app.link" in val


# ---------------------------------------------------------------------------
# PandoraProvider
# ---------------------------------------------------------------------------

class PandoraProvider(BaseProvider):
    name = "pandora"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s, retry=RetryConfig(max_attempts=2))
        self._async_http._headers.update({
            "Accept":     "application/json",
            "User-Agent": _MOBILE_UA,
        })
        self._limiter = async_songlink_rate_limiter

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get_json_async(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        merged = {
            "Accept":     "application/json",
            "User-Agent": _ua_for_url(url),
        }
        if headers:
            merged.update(headers)
            
        if self._limiter is not None:
            await self._limiter.wait_for_slot()

        resp = await self._async_http.get(url, headers=merged)
        return resp.json()

    def _post_json(self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        merged = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "User-Agent":   _ua_for_url(url),
        }
        if headers:
            merged.update(headers)
            
        try:
            resp = self._session.post(url, json=body, headers=merged, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"HTTP {e.response.status_code} for {url}") from e
        except (httpx.RequestError, ValueError) as e:
            raise RuntimeError(f"Request failed for {url}: {e}") from e

    async def _post_json_async(self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        merged = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "User-Agent":   _ua_for_url(url),
        }
        if headers:
            merged.update(headers)

        if self._limiter is not None:
            await self._limiter.wait_for_slot()

        try:
            resp = await self._async_http.post(url, json=body, headers=merged, timeout=30.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"HTTP {e.response.status_code} for {url}") from e
        except (httpx.RequestError, ValueError) as e:
            raise RuntimeError(f"Request failed for {url}: {e}") from e

    # ------------------------------------------------------------------
    # App link resolution
    # ------------------------------------------------------------------

    def check_availability(
        self,
        isrc: str,
        track_name: str,
        artist_name: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = options or {}

        direct_id = _extract_pandora_track_id(options.get("spotify_id"))
        if direct_id:
            return {"available": True, "track_id": direct_id}

        def _candidate(value: str | None) -> str:
            value = _normalize_secure_url(value)
            if not value:
                return ""
            if _HTTP_RE.match(value) or "pandora.com" in value or "pandora.app.link" in value:
                return value
            if _DIRECT_ID_RE.match(value):
                return _build_pandora_url(_normalize_pandora_id(value))
            if _SPOTIFY_BARE_ID_RE.match(value):
                return f"https://open.spotify.com/track/{value}"
            if _SPOTIFY_URI_RE.match(value):
                return "https://open.spotify.com/track/" + value.split(":")[-1]
            if _DEEZER_BARE_ID_RE.match(value):
                return f"https://www.deezer.com/track/{value}"
            return ""

        candidates = [
            _candidate(options.get("spotify_id")),
            _candidate(options.get("deezer_id")),
            _candidate(options.get("url")),
            _candidate(options.get("link")),
        ]

        for url in candidates:
            if not url:
                continue
            try:
                resolved_url = self._normalize_pandora_track_url(url)
                track_id = _extract_pandora_track_id(resolved_url)
                if track_id:
                    return {"available": True, "track_id": track_id}
            except Exception as exc:
                logger.debug("[pandora] availability candidate failed: %s", exc)

        return {"available": False, "reason": "not_found_on_pandora"}

    def _resolve_pandora_app_link(self, url: str) -> str:
        try:
            resp = self._session.get(
                _normalize_secure_url(url),
                headers={
                    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": _MOBILE_UA,
                },
                timeout=15,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.RequestError as e:
            raise RuntimeError(f"Pandora app link request failed: {e}") from e

        resp_url = str(resp.url)
        if "pandora.com/" in resp_url and "pandora.app.link" not in resp_url:
            return _normalize_secure_url(resp_url)

        resolved = _extract_pandora_url_from_html(resp.text)
        if not resolved:
            raise RuntimeError("Could not resolve Pandora app link")

        return resolved

    async def _resolve_pandora_app_link_async(self, url: str) -> str:
        try:
            resp = await self._async_http.get(
                _normalize_secure_url(url),
                headers={
                    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": _MOBILE_UA,
                },
                timeout=15.0,
                follow_redirects=True,
            )
        except httpx.RequestError as e:
            raise RuntimeError(f"Pandora app link request failed: {e}") from e

        resp_url = str(resp.url)
        if "pandora.com/" in resp_url and "pandora.app.link" not in resp_url:
            return _normalize_secure_url(resp_url)

        resolved = _extract_pandora_url_from_html(resp.text)
        if not resolved:
            raise RuntimeError("Could not resolve Pandora app link")

        return resolved

    def _normalize_pandora_input(self, input_url: str | None) -> str:
        normalized = (input_url or "").strip()
        if not normalized:
            return ""
        if _is_pandora_app_link(normalized):
            return self._resolve_pandora_app_link(normalized)
        return normalized

    # ------------------------------------------------------------------
    # Song.link resolution
    # ------------------------------------------------------------------

    def _resolve_song_link(self, url: str) -> dict[str, Any]:
        from ..core.http import songlink_rate_limiter
        params = {"url": url, "userCountry": _USER_COUNTRY}
        full_url = f"{_SONGLINK_URL}?{urlencode(params)}"
        songlink_rate_limiter.wait_for_slot()
        return self._get_json(full_url)

    def _extract_pandora_url_from_songlink(self, data: dict[str, Any] | None) -> str:
        links = (data or {}).get("linksByPlatform", {})
        url = links.get("pandora", {}).get("url")
        return _normalize_secure_url(url) if url else ""

    def _extract_entity_from_songlink(self, data: dict[str, Any] | None) -> dict[str, Any]:
        if not data:
            return {}
        unique_id = data.get("entityUniqueId")
        if unique_id:
            return data.get("entitiesByUniqueId", {}).get(unique_id, {})
        return {}

    def _extract_deezer_track_id_from_songlink(self, data: dict[str, Any] | None) -> str:
        url = (data or {}).get("linksByPlatform", {}).get("deezer", {}).get("url")
        if url:
            m = _DEEZER_TRACK_URL_RE.search(url)
            if m:
                return m.group(1)
        return ""

    # ------------------------------------------------------------------
    # Deezer enrichment helpers
    # ------------------------------------------------------------------

    def _fetch_deezer_track(self, track_id: str) -> dict[str, Any]:
        if not track_id:
            return {}
        try:
            return self._get_json(f"{_DEEZER_API_URL}/track/{quote(str(track_id))}")
        except Exception as exc:
            logger.debug("[pandora] Deezer track enrichment failed: %s", exc)
            return {}

    def _fetch_deezer_album(self, album_id: str | int) -> dict[str, Any]:
        if not album_id:
            return {}
        try:
            return self._get_json(f"{_DEEZER_API_URL}/album/{quote(str(album_id))}")
        except Exception as exc:
            logger.debug("[pandora] Deezer album enrichment failed: %s", exc)
            return {}

    @staticmethod
    def _normalize_artists_from_deezer(track: dict[str, Any]) -> str:
        if not track:
            return ""
        contributors = track.get("contributors", [])
        if contributors:
            names = [c["name"] for c in contributors if c.get("name")]
            if names:
                return ", ".join(names)
        return str(track.get("artist", {}).get("name", ""))

    @staticmethod
    def _normalize_album_artist_from_deezer(album: dict[str, Any], track: dict[str, Any]) -> str:
        if album and album.get("artist", {}).get("name"):
            return str(album["artist"]["name"])
        if track and track.get("artist", {}).get("name"):
            return str(track["artist"]["name"])
        return ""

    @staticmethod
    def _extract_deezer_genre(album: dict[str, Any] | None) -> str:
        genres = (album or {}).get("genres", {}).get("data", [])
        return ", ".join(g["name"] for g in genres if g.get("name")) if genres else ""

    @staticmethod
    def _extract_deezer_composer(track: dict[str, Any] | None) -> str:
        contributors = (track or {}).get("contributors", [])
        names = [
            c["name"] for c in contributors
            if c.get("name") and str(c.get("role", "")).lower() in ("composer", "author", "writer")
        ]
        return ", ".join(names)

    # ------------------------------------------------------------------
    # Track / Album resolution
    # ------------------------------------------------------------------

    def _resolve_pandora_track(self, input_url: str) -> dict[str, Any]:
        input_url = self._normalize_pandora_input(input_url)
        pandora_id = _extract_pandora_track_id(input_url)

        if not pandora_id:
            raise TrackNotFoundError(self.name, "Could not resolve Pandora track ID")

        source_url = input_url.strip()
        if not source_url or "pandora.com/" not in source_url:
            source_url = _build_pandora_url(pandora_id)

        pretty       = _parse_pandora_pretty_url(input_url)
        entity       = {}
        deezer_track = {}
        deezer_album = {}
        pandora_url  = _normalize_pandora_canonical_url(source_url, pandora_id)

        try:
            songlink = self._resolve_song_link(pandora_url)
            entity   = self._extract_entity_from_songlink(songlink)

            resolved_pandora_url = self._extract_pandora_url_from_songlink(songlink)
            if resolved_pandora_url:
                pandora_url = _normalize_pandora_canonical_url(resolved_pandora_url, pandora_id)

            resolved_id = _extract_pandora_track_id(pandora_url)
            if resolved_id:
                pandora_id = resolved_id

            if entity and entity.get("type") not in ("song", ""):
                raise RuntimeError("Resolved entity is not a Pandora track")

            deezer_track_id = self._extract_deezer_track_id_from_songlink(songlink)
            deezer_track = self._fetch_deezer_track(deezer_track_id)
            if deezer_track and deezer_track.get("album", {}).get("id"):
                deezer_album = self._fetch_deezer_album(deezer_track["album"]["id"])

        except Exception:
            if not pretty:
                raise
            logger.debug("[pandora] Song.link resolution failed (using pretty URL fallback)", exc_info=True)

        return {
            "pandoraID":   pandora_id,
            "pandoraURL":  pandora_url,
            "entity":      entity,
            "deezerTrack": deezer_track,
            "deezerAlbum": deezer_album,
            "pretty":      pretty or {},
        }

    def _resolve_pandora_album(self, input_url: str) -> dict[str, Any]:
        input_url  = self._normalize_pandora_input(input_url)
        pandora_id = _extract_pandora_album_id(input_url)

        if not pandora_id:
            raise SpotiflacError(ErrorKind.INVALID_URL, "Could not resolve Pandora album ID", self.name)

        source_url = input_url.strip()
        if not source_url or "pandora.com/" not in source_url:
            source_url = _build_pandora_url(pandora_id)

        pretty      = _parse_pandora_pretty_url(input_url)
        entity      = {}
        pandora_url = _normalize_pandora_canonical_url(source_url, pandora_id)

        try:
            songlink = self._resolve_song_link(pandora_url)
            entity   = self._extract_entity_from_songlink(songlink)

            resolved_pandora_url = self._extract_pandora_url_from_songlink(songlink)
            if resolved_pandora_url:
                pandora_url = _normalize_pandora_canonical_url(resolved_pandora_url, pandora_id)

            resolved_id = _extract_pandora_album_id(pandora_url)
            if resolved_id:
                pandora_id = resolved_id

            if entity and entity.get("type") not in ("album", ""):
                raise RuntimeError("Resolved entity is not a Pandora album")

        except Exception:
            if not pretty:
                raise
            logger.debug("[pandora] Song.link album resolution failed (pretty fallback)", exc_info=True)

        return {
            "pandoraID":  pandora_id,
            "pandoraURL": pandora_url,
            "entity":     entity,
            "pretty":     pretty or {},
        }

    # ------------------------------------------------------------------
    # Metadata builders
    # ------------------------------------------------------------------

    def _build_track_metadata(self, resolved: dict[str, Any]) -> TrackMetadata:
        entity       = resolved.get("entity", {})
        deezer_track = resolved.get("deezerTrack", {})
        deezer_album = resolved.get("deezerAlbum", {})
        pretty       = resolved.get("pretty", {})

        album = deezer_album if deezer_album.get("id") else (deezer_track.get("album", {}) or {})
        
        album_artist = (
            self._normalize_album_artist_from_deezer(deezer_album, deezer_track)
            or entity.get("artistName")
            or pretty.get("artistName")
            or ""
        )
        
        release_date = deezer_track.get("release_date") or album.get("release_date") or ""
        total_tracks = album.get("nb_tracks", 0)
        composer     = self._extract_deezer_composer(deezer_track)

        title = (
            deezer_track.get("title")
            or entity.get("title")
            or pretty.get("trackName")
            or resolved.get("pandoraID", "Unknown")
        )
        artists = (
            self._normalize_artists_from_deezer(deezer_track)
            or entity.get("artistName")
            or pretty.get("artistName")
            or "Unknown"
        )
        cover_url = (
            album.get("cover_xl")
            or album.get("cover_big")
            or album.get("cover_medium")
            or entity.get("thumbnailUrl")
            or ""
        )
        
        album_title = album.get("title") or pretty.get("albumName") or "Unknown"

        return TrackMetadata(
            id           = resolved.get("pandoraID", ""),
            title        = title,
            artists      = artists,
            album        = album_title,
            album_artist = album_artist or artists,
            isrc         = deezer_track.get("isrc", ""),
            track_number = deezer_track.get("track_position", 0),
            disc_number  = deezer_track.get("disk_number", 1),
            total_tracks = total_tracks,
            total_discs  = deezer_track.get("disk_number", 1),
            duration_ms  = int(deezer_track.get("duration", 0)) * 1000,
            release_date = release_date,
            cover_url    = cover_url,
            external_url = resolved.get("pandoraURL", ""),
            genre        = self._extract_deezer_genre(deezer_album),
            publisher    = deezer_album.get("label", ""),
            composer     = composer,
            extra_info   = {"provider": "pandora"},
        )

    def _build_album_metadata(self, resolved: dict[str, Any]) -> dict[str, Any]:
        entity = resolved.get("entity", {})
        pretty = resolved.get("pretty", {})
        return {
            "id":           resolved.get("pandoraID", ""),
            "name":         entity.get("title") or pretty.get("albumName") or resolved.get("pandoraID", ""),
            "artists":      entity.get("artistName") or pretty.get("artistName") or "",
            "cover_url":    entity.get("thumbnailUrl", ""),
            "release_date": "",
            "total_tracks": 0,
            "pandora_url":  resolved.get("pandoraURL", ""),
        }

    # ------------------------------------------------------------------
    # get_url
    # ------------------------------------------------------------------

    def get_url(self, url: str) -> tuple[str, list[TrackMetadata]]:
        input_url = self._normalize_pandora_input(url)

        if "pandora.com" not in input_url:
            raise SpotiflacError(ErrorKind.INVALID_URL, f"Not a Pandora URL: {url}", self.name)

        pretty = _parse_pandora_pretty_url(input_url)

        track_id = _extract_pandora_track_id(input_url)
        if track_id or (pretty and pretty.get("type") == "track") or "pandora.com/" in input_url:
            try:
                resolved = self._resolve_pandora_track(input_url)
                meta     = self._build_track_metadata(resolved)
                return meta.title, [meta]
            except Exception as exc:
                logger.debug("[pandora] Track resolution failed: %s", exc)

        album_id = _extract_pandora_album_id(input_url)
        if album_id or (pretty and pretty.get("type") == "album"):
            try:
                resolved     = self._resolve_pandora_album(input_url)
                album_meta   = self._build_album_metadata(resolved)
                name         = album_meta.get("name", "Unknown Album")
                logger.info("[pandora] Album resolved: %s (no track list — Pandora API limitation)", name)
                return name, []
            except Exception as exc:
                logger.debug("[pandora] Album resolution failed: %s", exc)

        raise SpotiflacError(
            ErrorKind.INVALID_URL,
            f"Could not resolve Pandora URL: {url}",
            self.name,
        )

    def _normalize_pandora_track_url(self, input_url: str) -> str:
        input_url = self._normalize_pandora_input(input_url)
        track_id  = _extract_pandora_track_id(input_url)
        
        if track_id:
            return _normalize_pandora_canonical_url(input_url, track_id)

        url_for_songlink = input_url if input_url.startswith("http") else f"https://open.spotify.com/track/{input_url}"
        
        songlink    = self._resolve_song_link(url_for_songlink)
        pandora_url = self._extract_pandora_url_from_songlink(songlink)
        resolved_id = _extract_pandora_track_id(pandora_url)

        if not resolved_id:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                "Could not resolve Pandora track URL",
                self.name,
            )

        return _normalize_pandora_canonical_url(pandora_url, resolved_id)

    async def _normalize_pandora_track_url_async(self, input_url: str) -> str:
        input_url = (input_url or "").strip()
        if _is_pandora_app_link(input_url):
            input_url = await self._resolve_pandora_app_link_async(input_url)

        track_id = _extract_pandora_track_id(input_url)
        if track_id:
            return _normalize_pandora_canonical_url(input_url, track_id)

        url_for_songlink = input_url if input_url.startswith("http") else f"https://open.spotify.com/track/{input_url}"
        params = {"url": url_for_songlink, "userCountry": _USER_COUNTRY}
        songlink = await self._get_json_async(f"{_SONGLINK_URL}?{urlencode(params)}")
        pandora_url = self._extract_pandora_url_from_songlink(songlink)
        resolved_id = _extract_pandora_track_id(pandora_url)

        if not resolved_id:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                "Could not resolve Pandora track URL",
                self.name,
            )

        return _normalize_pandora_canonical_url(pandora_url, resolved_id)

    # ----------------------------------------

    async def download_track_async(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            filename_format:     str              = "{title} - {artist}",
            position:            int              = 1,
            include_track_num:   bool             = False,
            use_album_track_num: bool             = False,
            first_artist_only:   bool             = False,
            allow_fallback:      bool             = True,
            quality:             str              = "mp3_192",
            embed_lyrics:        bool             = False,
            lyrics_providers:    list[str] | None = None,
            enrich_metadata:     bool             = False,
            enrich_providers:    list[str] | None = None,
            qobuz_token:         str | None       = None,
            is_album:            bool             = False,
            **kwargs,
    ) -> DownloadResult:
        try:
            pandora_track_id = str(metadata.id or "").strip()

            if metadata.external_url and "pandora.com" in metadata.external_url:
                download_url = _normalize_secure_url(metadata.external_url)
            elif pandora_track_id:
                resolve_input = (
                    metadata.external_url
                    if metadata.external_url and metadata.external_url.startswith("http")
                    else pandora_track_id
                )
                download_url = _normalize_secure_url(
                    await self._normalize_pandora_track_url_async(resolve_input)
                )
            else:
                return DownloadResult.fail(self.name, "No Pandora track ID or URL available")

            dest_mp3 = Path(self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num,
                first_artist_only, extension=".mp3",
            ))
            dest_m4a = dest_mp3.with_suffix(".m4a")

            for candidate in (dest_mp3, dest_m4a):
                if self._file_exists(candidate):
                    fmt = "mp3" if candidate.suffix == ".mp3" else "m4a"
                    return DownloadResult.skipped_result(self.name, str(candidate), fmt=fmt)

            import concurrent.futures
            from ..core.isrc_utils import normalize_isrc
            _isrc_for_mb = normalize_isrc(getattr(metadata, "isrc", None) or "")
            logger.debug("[pandora] ISRC at MB lookup: %r", _isrc_for_mb)
            mb_fetcher = AsyncMBFetch(_isrc_for_mb) if _isrc_for_mb else None
            if not mb_fetcher:
                logger.warning("[pandora] MusicBrainz skipped: no valid ISRC available")


            pan_base, pan_path = get_pandora_base_and_path()
            pan_full_url = f"{pan_base}{pan_path}"

            print_source_banner("pandora", "", quality)

            zarz_id = _extract_pandora_track_id(download_url)
            safe_api_url = _build_pandora_url(zarz_id) if zarz_id else download_url

            payload = await self._post_json_async(
                pan_full_url,
                {"url": safe_api_url},
            )

            if not payload or payload.get("success") is not True:
                error_msg = "Pandora API request failed"
                err_data = payload.get("error")
                if isinstance(err_data, dict):
                    error_msg = err_data.get("message", error_msg)
                elif isinstance(err_data, str):
                    error_msg = err_data
                return DownloadResult.fail(self.name, error_msg)

            cdn_links = payload.get("cdnLinks", {})
            q_norm = normalize_quality(quality) if isinstance(quality, str) else quality
            if q_norm == "HIGH":
                pandora_token = "mp3_192"
            elif q_norm == "LOW":
                pandora_token = "aac_32"
            else:
                pandora_token = "mp3_192"

            selected = _select_quality_link(cdn_links, pandora_token)
            if not selected or not selected.get("url"):
                if allow_fallback:
                    for fallback_q in ("mp3_192", "aac_64", "aac_32"):
                        if fallback_q != quality:
                            selected = _select_quality_link(cdn_links, fallback_q)
                            if selected and selected.get("url"):
                                logger.info("[pandora] Quality fallback: %s → %s", quality, fallback_q)
                                break
                if not selected or not selected.get("url"):
                    return DownloadResult.fail(self.name, "No downloadable Pandora stream available")

            stream_url = _normalize_secure_url(selected["url"])
            ext = _output_extension_for_link(selected)
            fmt_str = "mp3" if ext == ".mp3" else "m4a"
            dest = dest_mp3.with_suffix(ext)

            os.makedirs(output_dir, exist_ok=True)
            await self._async_http.stream_to_file(
                stream_url,
                str(dest),
                self._progress_cb,
                extra_headers={"User-Agent": _ua_for_url(stream_url)}
            )

            expected_s = metadata.duration_ms // 1000
            valid, err_msg = await validate_downloaded_track_async(str(dest), expected_s)
            if not valid:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, err_msg, self.name)

            mb_tags: dict[str, str] = {}
            if mb_fetcher:
                try:
                    mb_result = await asyncio.to_thread(lambda: mb_fetcher.future.result(timeout=12))
                    mb_tags = mb_result_to_tags(mb_result)
                    if mb_tags:
                        logger.info("[pandora] MusicBrainz tags found: %s", list(mb_tags.keys()))
                        _print_mb_summary(mb_tags)
                    else:
                        logger.warning("[pandora] MusicBrainz returned no tags (ISRC: %r)", _isrc_for_mb)
                except concurrent.futures.TimeoutError:
                    logger.warning("[pandora] MusicBrainz timed out after 12s, skipping MB tags")
                except Exception as exc:
                    logger.warning("[pandora] MusicBrainz error: %s", exc)

            opts = EmbedOptions(
                first_artist_only  = first_artist_only,
                cover_url          = metadata.cover_url,
                embed_lyrics       = embed_lyrics,
                lyrics_providers   = lyrics_providers or [],
                enrich             = enrich_metadata,
                enrich_providers   = enrich_providers,
                enrich_qobuz_token = qobuz_token or "",
                is_album           = is_album,
                extra_tags         = mb_tags,
            )
            await embed_metadata_async(str(dest), metadata, opts)

            return DownloadResult.ok(self.name, str(dest), fmt=fmt_str)

        except SpotiflacError as exc:
            logger.error("[pandora] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[pandora] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")


# ---------------------------------------------------------------------------
# URL detection helper
# ---------------------------------------------------------------------------

def is_pandora_url(url: str) -> bool:
    return _is_pandora_url(url)


def parse_pandora_url(url: str) -> dict[str, str]:
    try:
        input_url = url.strip()

        if _is_pandora_app_link(input_url):
            return {"type": "track", "id": input_url}

        pretty = _parse_pandora_pretty_url(input_url)
        if pretty:
            return {"type": pretty.get("type", "track"), "id": input_url}

        track_id = _extract_pandora_track_id(input_url)
        if track_id:
            return {"type": "track", "id": track_id}

        album_id = _extract_pandora_album_id(input_url)
        if album_id:
            return {"type": "album", "id": album_id}

    except Exception:
        pass

    return {"type": "track", "id": url}

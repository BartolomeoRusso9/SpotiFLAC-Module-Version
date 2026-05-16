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

import logging
import os
import re
from typing import Callable
from urllib.parse import urlparse, urlencode, quote

import requests

from .base import BaseProvider
from ..core.console import print_source_banner
from ..core.download_validation import validate_downloaded_track
from ..core.errors import SpotiflacError, ErrorKind, TrackNotFoundError
from ..core.http import RetryConfig
from ..core.models import TrackMetadata, DownloadResult
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags
from ..core.tagger import embed_metadata, _print_mb_summary, EmbedOptions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_BASE_URL    = "https://api.zarz.moe"
_DOWNLOAD_PATH   = "/v1/dl/pan"
_SONGLINK_URL    = "https://api.song.link/v1-alpha.1/links"
_DEEZER_API_URL  = "https://api.deezer.com"
_PANDORA_BASE    = "https://www.pandora.com"
_USER_COUNTRY    = "US"

_MOBILE_UA = "SpotiFLAC-Mobile"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# Pandora ID regex: TR:xxx, AL:xxx, AR:xxx, PL:xxx, ST:xxx
_PANDORA_ID_RE       = re.compile(r'\b(TR|AL|AR|PL|ST):[A-Za-z0-9:]+\b', re.IGNORECASE)
_PANDORA_PRETTY_RE   = re.compile(r'(?:^|[/?=&])((TR|AL|AR|PL|ST)[A-Za-z0-9]+)(?=$|[/?&#])', re.IGNORECASE)

_QUALITY_MAP = {
    "mp3_192": "highQuality",
    "aac_64":  "mediumQuality",
    "aac_32":  "lowQuality",
}


# ---------------------------------------------------------------------------
# URL / ID helpers (porting da index.js)
# ---------------------------------------------------------------------------

def _normalize_secure_url(url: str) -> str:
    url = str(url or "").strip()
    if re.match(r'^http://', url, re.IGNORECASE):
        return re.sub(r'^http://', 'https://', url, flags=re.IGNORECASE)
    return url


def _strip_url_query(url: str) -> str:
    return re.sub(r'[?#].*$', '', str(url or ''))


def _title_case_from_slug(value: str) -> str:
    """Converte un slug URL in Title Case (es. 'rock-and-roll' → 'Rock And Roll')."""
    value = str(value or "").strip()
    if not value:
        return ""
    cleaned = re.sub(r'[-_]+', ' ', value)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return re.sub(r'\b\w', lambda m: m.group(0).upper(), cleaned)


def _try_parse_url(url: str) -> dict | None:
    """Parsifica un URL e restituisce hostname e pathname."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        return {
            "hostname": parsed.hostname or "",
            "pathname": parsed.path or "/",
        }
    except Exception:
        m = re.match(r'^https?://([^/]+)(/[^?#]*)?', str(url or ''), re.IGNORECASE)
        if not m:
            return None
        return {"hostname": m.group(1) or "", "pathname": m.group(2) or "/"}


def _normalize_pandora_id(value: str) -> str:
    """Estrae e normalizza un Pandora ID (TR:..., AL:...) dall'input."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    try:
        from urllib.parse import unquote
        raw = unquote(raw)
    except Exception:
        pass

    # Pattern con separatore : (TR:XXXXXX)
    m = _PANDORA_ID_RE.search(raw)
    if m:
        return m.group(0).upper()

    # Pattern senza separatore : (TRXXXXXX)
    pm = _PANDORA_PRETTY_RE.search(raw)
    return pm.group(1) if pm else ""


def _extract_pandora_track_id(value: str) -> str:
    """Restituisce il track ID se inizia con TR:, altrimenti stringa vuota."""
    pid = _normalize_pandora_id(value)
    return pid if re.match(r'^TR(?::)?', pid, re.IGNORECASE) else ""


def _extract_pandora_album_id(value: str) -> str:
    """Restituisce l'album ID se inizia con AL:, altrimenti stringa vuota."""
    pid = _normalize_pandora_id(value)
    return pid if re.match(r'^AL(?::)?', pid, re.IGNORECASE) else ""


def _build_pandora_url(pandora_id: str) -> str:
    return f"{_PANDORA_BASE}/{pandora_id.strip()}"


def _normalize_pandora_web_url(url: str) -> str:
    """
    Porta di normalizePandoraWebURL() in JS.
    Ricostruisce l'URL canonico per URL Pandora pretty (con slugs).
    """
    normalized = _strip_url_query(_normalize_secure_url(url))
    parsed = _try_parse_url(normalized)
    if not parsed or "pandora.com" not in parsed["hostname"].lower():
        return normalized

    segments = [s for s in parsed["pathname"].strip("/").split("/") if s]
    if not segments:
        return normalized

    if segments[0] in ("artist", "playlist"):
        return normalized

    last = segments[-1] if segments else ""
    if (re.match(r'^TR', last, re.IGNORECASE) and len(segments) >= 4) or \
       (re.match(r'^AL', last, re.IGNORECASE) and len(segments) >= 3):
        return _PANDORA_BASE + "/artist/" + "/".join(segments)

    return normalized


def _normalize_pandora_canonical_url(input_url: str, pandora_id: str) -> str:
    normalized = _normalize_secure_url(str(input_url or "").strip())

    if "pandora.com/" in normalized and _extract_pandora_track_id(normalized):
        return _normalize_pandora_web_url(normalized)
    if "pandora.com/" in normalized and _extract_pandora_album_id(normalized):
        return _normalize_pandora_web_url(normalized)

    return _build_pandora_url(pandora_id)


def _is_pandora_app_link(url: str) -> bool:
    parsed = _try_parse_url(url)
    if not parsed or not parsed["hostname"]:
        return False
    return parsed["hostname"].lower() == "pandora.app.link"


def _extract_pandora_url_from_html(html: str) -> str:
    """
    Porta di extractPandoraURLFromAppLinkHTML() in JS.
    Cerca l'URL Pandora nell'HTML dell'app link.
    """
    body = str(html or "")
    if not body:
        return ""

    matches = re.findall(
        r'https?://(?:www\.)?pandora\.com/[^"\'<>\\\s]+',
        body, re.IGNORECASE
    )
    for candidate in matches:
        normalized = _normalize_secure_url(candidate).replace("&amp;", "&")
        if "pandora.com/" in normalized:
            return normalized

    # Fallback: cerca pandoraId= nel body
    m = re.search(r'pandoraId=([^"\'&<>\s]+)', body, re.IGNORECASE)
    if m:
        decoded = m.group(1)
        try:
            from urllib.parse import unquote
            decoded = unquote(decoded)
        except Exception:
            pass
        pid = _normalize_pandora_id(decoded)
        if pid:
            return _build_pandora_url(pid)

    return ""


def _parse_pandora_pretty_url(url: str) -> dict | None:
    """
    Porta di parsePandoraPrettyURL() in JS.
    Estrae tipo e nomi leggibili dall'URL Pandora.
    """
    if not url:
        return None
    try:
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
            if len(segments) >= 4 and re.match(r'^AL', segments[3], re.IGNORECASE):
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
    except Exception:
        pass

    return None


def _select_quality_link(cdn_links: dict, quality: str) -> dict | None:
    """
    Porta di selectQualityLink() in JS.
    Seleziona il link CDN appropriato per la qualità richiesta.
    """
    requested = str(quality or "mp3_192").lower()

    if requested == "aac_64" and cdn_links.get("mediumQuality"):
        return cdn_links["mediumQuality"]
    if requested == "aac_32" and cdn_links.get("lowQuality"):
        return cdn_links["lowQuality"]
    if cdn_links.get("highQuality"):
        return cdn_links["highQuality"]
    if cdn_links.get("mediumQuality"):
        return cdn_links["mediumQuality"]
    if cdn_links.get("lowQuality"):
        return cdn_links["lowQuality"]

    return None


def _output_extension_for_link(link_info: dict) -> str:
    """Porta di outputExtensionForLink() in JS."""
    if not link_info:
        return ".bin"

    encoding = str(link_info.get("encoding", "")).lower()
    if encoding in ("mp3", "mpeg"):
        return ".mp3"
    if "aac" in encoding:
        return ".m4a"

    url = str(link_info.get("url", ""))
    if re.search(r'\.mp3(?:$|\?)', url, re.IGNORECASE):
        return ".mp3"
    if re.search(r'\.m4a(?:$|\?)', url, re.IGNORECASE):
        return ".m4a"
    if re.search(r'\.mp4(?:$|\?)', url, re.IGNORECASE):
        return ".m4a"

    return ".bin"


def _ua_for_url(url: str) -> str:
    """Restituisce il giusto User-Agent in base all'URL (porta di userAgentForURL)."""
    text = str(url or "").strip().lower()
    if text.startswith("https://api.zarz.moe") or text.startswith("http://api.zarz.moe"):
        return _MOBILE_UA
    return _DEFAULT_UA


def _is_pandora_url(url: str) -> bool:
    return "pandora.com" in str(url or "").lower() or "pandora.app.link" in str(url or "").lower()


# ---------------------------------------------------------------------------
# PandoraProvider
# ---------------------------------------------------------------------------

class PandoraProvider(BaseProvider):
    """
    Provider Pandora per SpotiFLAC.

    Flusso principale:
      1. Normalizza l'URL/ID di input (risolve app link, pretty URL, ecc.)
      2. Usa Song.link per trovare l'URL Pandora canonico e l'ID Deezer
      3. Arricchisce i metadati con l'API Deezer (genere, label, ISRC, copertina HD)
      4. Invia POST a api.zarz.moe/v1/dl/pan con l'URL Pandora
      5. Seleziona il link CDN appropriato per la qualità richiesta
      6. Scarica il file e applica la pipeline completa (MusicBrainz, lyrics, tagging)
    """
    name = "pandora"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s, retry=RetryConfig(max_attempts=2))
        self._session = requests.Session()
        self._session.headers.update({
            "Accept":     "application/json",
            "User-Agent": _MOBILE_UA,
        })

    def set_progress_callback(self, cb: Callable[[int, int], None]) -> None:
        super().set_progress_callback(cb)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_json(self, url: str, headers: dict | None = None) -> dict:
        merged = {
            "Accept":     "application/json",
            "User-Agent": _ua_for_url(url),
        }
        if headers:
            merged.update(headers)
        resp = self._session.get(url, headers=merged, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} for {url}")
        return resp.json()

    def _post_json(self, url: str, body: dict, headers: dict | None = None) -> dict:
        merged = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "User-Agent":   _ua_for_url(url),
        }
        if headers:
            merged.update(headers)
        resp = self._session.post(url, json=body, headers=merged, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} for {url}")
        return resp.json()

    # ------------------------------------------------------------------
    # App link resolution
    # ------------------------------------------------------------------

    def _resolve_pandora_app_link(self, url: str) -> str:
        """Porta di resolvePandoraAppLink() in JS."""
        resp = self._session.get(
            _normalize_secure_url(url),
            headers={
                "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": _MOBILE_UA,
            },
            timeout=15,
            allow_redirects=True,
        )

        # Se il redirect porta già a pandora.com, usiamo l'URL finale
        if resp.url and "pandora.com/" in resp.url and "pandora.app.link" not in resp.url:
            return _normalize_secure_url(resp.url)

        if resp.status_code != 200:
            raise RuntimeError(f"Pandora app link returned HTTP {resp.status_code}")

        resolved = _extract_pandora_url_from_html(resp.text or "")
        if not resolved:
            raise RuntimeError("Could not resolve Pandora app link")

        return resolved

    def _normalize_pandora_input(self, input_url: str) -> str:
        """Porta di normalizePandoraInput() in JS."""
        normalized = str(input_url or "").strip()
        if not normalized:
            return ""
        if _is_pandora_app_link(normalized):
            return self._resolve_pandora_app_link(normalized)
        return normalized

    # ------------------------------------------------------------------
    # Song.link resolution
    # ------------------------------------------------------------------

    def _resolve_song_link(self, url: str) -> dict:
        """Porta di resolveSongLink() in JS."""
        params = {
            "url":         url,
            "userCountry": _USER_COUNTRY,
        }
        full_url = f"{_SONGLINK_URL}?{urlencode(params)}"
        return self._get_json(full_url)

    def _extract_pandora_url_from_songlink(self, data: dict) -> str:
        links = (data or {}).get("linksByPlatform", {})
        pandora = links.get("pandora", {})
        url = pandora.get("url", "")
        return _normalize_secure_url(url) if url else ""

    def _extract_entity_from_songlink(self, data: dict) -> dict:
        if not data:
            return {}
        unique_id = data.get("entityUniqueId", "")
        entities  = data.get("entitiesByUniqueId", {})
        if unique_id and unique_id in entities:
            return entities[unique_id]
        return {}

    def _extract_deezer_track_id_from_songlink(self, data: dict) -> str:
        links = (data or {}).get("linksByPlatform", {})
        deezer = links.get("deezer", {})
        deezer_url = deezer.get("url", "")
        if deezer_url:
            m = re.search(r'deezer\.com/(?:[a-z]{2}/)?track/(\d+)', deezer_url, re.IGNORECASE)
            if m:
                return m.group(1)
        return ""

    # ------------------------------------------------------------------
    # Deezer enrichment helpers
    # ------------------------------------------------------------------

    def _fetch_deezer_track(self, track_id: str) -> dict:
        if not track_id:
            return {}
        try:
            return self._get_json(f"{_DEEZER_API_URL}/track/{quote(str(track_id))}")
        except Exception as exc:
            logger.debug("[pandora] Deezer track enrichment failed: %s", exc)
            return {}

    def _fetch_deezer_album(self, album_id: str | int) -> dict:
        if not album_id:
            return {}
        try:
            return self._get_json(f"{_DEEZER_API_URL}/album/{quote(str(album_id))}")
        except Exception as exc:
            logger.debug("[pandora] Deezer album enrichment failed: %s", exc)
            return {}

    @staticmethod
    def _normalize_artists_from_deezer(track: dict) -> str:
        """Porta di normalizeArtistsFromDeezer() in JS."""
        if not track:
            return ""
        contributors = track.get("contributors", [])
        if contributors:
            names = [c["name"] for c in contributors if c.get("name")]
            if names:
                return ", ".join(names)
        artist = track.get("artist", {})
        return str(artist.get("name", "")) if artist else ""

    @staticmethod
    def _normalize_album_artist_from_deezer(album: dict, track: dict) -> str:
        """Porta di normalizeAlbumArtistFromDeezer() in JS."""
        if album and album.get("artist", {}).get("name"):
            return str(album["artist"]["name"])
        if track and track.get("artist", {}).get("name"):
            return str(track["artist"]["name"])
        return ""

    @staticmethod
    def _extract_deezer_genre(album: dict) -> str:
        genres = (album or {}).get("genres", {}).get("data", [])
        if not genres:
            return ""
        return ", ".join(g["name"] for g in genres if g.get("name"))

    @staticmethod
    def _extract_deezer_composer(track: dict) -> str:
        contributors = (track or {}).get("contributors", [])
        names = [
            c["name"] for c in contributors
            if c.get("name") and str(c.get("role", "")).lower() in ("composer", "author", "writer")
        ]
        return ", ".join(names)

    # ------------------------------------------------------------------
    # Track / Album resolution (porta di resolvePandoraTrack / Album)
    # ------------------------------------------------------------------

    def _resolve_pandora_track(self, input_url: str) -> dict:
        """
        Porta di resolvePandoraTrack() in JS.
        Restituisce un dizionario con pandoraID, pandoraURL, entity, deezerTrack, deezerAlbum, pretty.
        """
        input_url = self._normalize_pandora_input(input_url)
        pandora_id = _extract_pandora_track_id(input_url)

        if not pandora_id:
            raise TrackNotFoundError(self.name, "Could not resolve Pandora track ID")

        source_url = str(input_url or "").strip()
        if not source_url or "pandora.com/" not in source_url:
            source_url = _build_pandora_url(pandora_id)

        pretty       = _parse_pandora_pretty_url(input_url)
        songlink     = None
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

        except Exception as exc:
            if not pretty:
                raise
            logger.debug("[pandora] Song.link resolution failed (using pretty URL fallback): %s", exc)

        return {
            "pandoraID":   pandora_id,
            "pandoraURL":  pandora_url,
            "entity":      entity,
            "deezerTrack": deezer_track,
            "deezerAlbum": deezer_album,
            "pretty":      pretty or {},
        }

    def _resolve_pandora_album(self, input_url: str) -> dict:
        """Porta di resolvePandoraAlbum() in JS."""
        input_url  = self._normalize_pandora_input(input_url)
        pandora_id = _extract_pandora_album_id(input_url)

        if not pandora_id:
            raise SpotiflacError(ErrorKind.INVALID_URL, "Could not resolve Pandora album ID", self.name)

        source_url = str(input_url or "").strip()
        if not source_url or "pandora.com/" not in source_url:
            source_url = _build_pandora_url(pandora_id)

        pretty      = _parse_pandora_pretty_url(input_url)
        songlink    = None
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

        except Exception as exc:
            if not pretty:
                raise
            logger.debug("[pandora] Song.link album resolution failed (pretty fallback): %s", exc)

        return {
            "pandoraID":  pandora_id,
            "pandoraURL": pandora_url,
            "entity":     entity,
            "pretty":     pretty or {},
        }

    # ------------------------------------------------------------------
    # Metadata builders (porta di buildTrackMetadata / buildAlbumMetadata)
    # ------------------------------------------------------------------

    def _build_track_metadata(self, resolved: dict) -> TrackMetadata:
        """Porta di buildTrackMetadata() in JS → TrackMetadata Pydantic."""
        entity       = resolved.get("entity", {})   or {}
        deezer_track = resolved.get("deezerTrack", {}) or {}
        deezer_album = resolved.get("deezerAlbum", {}) or {}
        pretty       = resolved.get("pretty", {})   or {}

        album = deezer_album if deezer_album.get("id") else (deezer_track.get("album", {}) or {})
        album_artist = (
            self._normalize_album_artist_from_deezer(deezer_album, deezer_track)
            or entity.get("artistName", "")
            or pretty.get("artistName", "")
        )
        release_date = (
            deezer_track.get("release_date", "")
            or album.get("release_date", "")
        )
        total_tracks = album.get("nb_tracks", 0) or 0
        composer     = self._extract_deezer_composer(deezer_track)

        title = (
            deezer_track.get("title", "")
            or entity.get("title", "")
            or pretty.get("trackName", "")
            or resolved.get("pandoraID", "")
        )
        artists = (
            self._normalize_artists_from_deezer(deezer_track)
            or entity.get("artistName", "")
            or pretty.get("artistName", "")
        )
        cover_url = (
            album.get("cover_xl", "")
            or album.get("cover_big", "")
            or album.get("cover_medium", "")
            or entity.get("thumbnailUrl", "")
        )
        album_title = album.get("title", "") or pretty.get("albumName", "")
        genre       = self._extract_deezer_genre(deezer_album)
        label       = deezer_album.get("label", "")
        isrc        = deezer_track.get("isrc", "")

        return TrackMetadata(
            id           = resolved.get("pandoraID", ""),
            title        = title or "Unknown",
            artists      = artists or "Unknown",
            album        = album_title or "Unknown",
            album_artist = album_artist or artists or "Unknown",
            isrc         = isrc,
            track_number = deezer_track.get("track_position", 0) or 0,
            disc_number  = deezer_track.get("disk_number", 1) or 1,
            total_tracks = total_tracks,
            total_discs  = deezer_track.get("disk_number", 1) or 1,
            duration_ms  = int(deezer_track.get("duration", 0) or 0) * 1000,
            release_date = release_date,
            cover_url    = cover_url,
            external_url = resolved.get("pandoraURL", ""),
            genre        = genre,
            publisher    = label,
            composer     = composer,
            extra_info   = {"provider": "pandora"},
        )

    def _build_album_metadata(self, resolved: dict) -> dict:
        entity = resolved.get("entity", {}) or {}
        pretty = resolved.get("pretty", {}) or {}
        return {
            "id":          resolved.get("pandoraID", ""),
            "name":        entity.get("title", "") or pretty.get("albumName", "") or resolved.get("pandoraID", ""),
            "artists":     entity.get("artistName", "") or pretty.get("artistName", ""),
            "cover_url":   entity.get("thumbnailUrl", ""),
            "release_date": "",
            "total_tracks": 0,
            "pandora_url": resolved.get("pandoraURL", ""),
        }

    # ------------------------------------------------------------------
    # get_url — entry point per la raccolta metadati (traccia/album)
    # ------------------------------------------------------------------

    def get_url(self, url: str) -> tuple[str, list[TrackMetadata]]:
        """
        Risolve un URL Pandora e restituisce (nome_collezione, [TrackMetadata]).
        Supporta: tracce, album, pretty URL, app link.
        """
        input_url = self._normalize_pandora_input(url)

        if "pandora.com" not in input_url:
            raise SpotiflacError(ErrorKind.INVALID_URL, f"Not a Pandora URL: {url}", self.name)

        pretty = _parse_pandora_pretty_url(input_url)

        # 1. Prova come traccia
        track_id = _extract_pandora_track_id(input_url)
        if track_id or (pretty and pretty.get("type") == "track") or "pandora.com/" in input_url:
            try:
                resolved = self._resolve_pandora_track(input_url)
                meta     = self._build_track_metadata(resolved)
                return meta.title, [meta]
            except Exception as exc:
                logger.debug("[pandora] Track resolution failed: %s", exc)

        # 2. Prova come album
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

    # ------------------------------------------------------------------
    # normalizePandoraTrackURL (porta di normalizePandoraTrackURL in JS)
    # ------------------------------------------------------------------

    def _normalize_pandora_track_url(self, input_url: str) -> str:
        """
        Restituisce l'URL canonico Pandora per una traccia.
        Se non c'è un track ID diretto, usa Song.link per risolverlo.
        """
        input_url  = self._normalize_pandora_input(input_url)
        track_id   = _extract_pandora_track_id(input_url)
        if track_id:
            return _normalize_pandora_canonical_url(input_url, track_id)

        # Fallback via Song.link
        songlink   = self._resolve_song_link(str(input_url or "").strip())
        pandora_url = self._extract_pandora_url_from_songlink(songlink)
        resolved_id = _extract_pandora_track_id(pandora_url)

        if not resolved_id:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                "Could not resolve Pandora track URL",
                self.name,
            )

        return _normalize_pandora_canonical_url(pandora_url, resolved_id)

    # ------------------------------------------------------------------
    # BaseProvider.download_track
    # ------------------------------------------------------------------

    def download_track(
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
            # 1. Determina l'URL Pandora canonico da scaricare
            #    metadata.id contiene il pandoraID (es. TR:12345)
            #    metadata.external_url contiene l'URL canonico (se disponibile)
            pandora_track_id = str(metadata.id or "").strip()

            if metadata.external_url and "pandora.com" in metadata.external_url:
                download_url = _normalize_secure_url(metadata.external_url)
            elif pandora_track_id:
                download_url = _normalize_secure_url(
                    self._normalize_pandora_track_url(pandora_track_id)
                )
            else:
                return DownloadResult.fail(self.name, "No Pandora track ID or URL available")

            # 2. Costruisci percorso output — Pandora può restituire MP3 o M4A
            #    Usiamo .mp3 come estensione temporanea, verrà corretta dopo
            dest_mp3 = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num,
                first_artist_only, extension=".mp3",
            )
            dest_m4a = dest_mp3.with_suffix(".m4a")

            # Controlla se esiste già in uno dei formati
            for candidate in (dest_mp3, dest_m4a):
                if self._file_exists(candidate):
                    fmt = "mp3" if candidate.suffix == ".mp3" else "m4a"
                    return DownloadResult.ok(self.name, str(candidate), fmt=fmt)

            # 3. Avvia MusicBrainz in background
            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None

            # 4. Chiama l'API Zarz per ottenere i CDN links
            print_source_banner("pandora", f"{_API_BASE_URL}{_DOWNLOAD_PATH}", quality)

            payload = self._post_json(
                f"{_API_BASE_URL}{_DOWNLOAD_PATH}",
                {"url": download_url},
            )

            if not payload or payload.get("success") is not True:
                error_msg = "Pandora API request failed"
                if payload and isinstance(payload.get("error"), dict):
                    error_msg = payload["error"].get("message", error_msg)
                elif payload and isinstance(payload.get("error"), str):
                    error_msg = payload["error"]
                return DownloadResult.fail(self.name, error_msg)

            cdn_links = payload.get("cdnLinks", {})
            selected  = _select_quality_link(cdn_links, quality)

            if not selected or not selected.get("url"):
                # Fallback automatico se la qualità richiesta non è disponibile
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
            ext        = _output_extension_for_link(selected)   # ".mp3" o ".m4a"
            fmt_str    = "mp3" if ext == ".mp3" else "m4a"

            # Aggiusta l'estensione del percorso finale
            dest = dest_mp3.with_suffix(ext)

            # 5. Scarica il file audio
            with self._session.get(
                stream_url,
                headers={"User-Agent": _ua_for_url(stream_url)},
                stream=True,
                timeout=120,
            ) as r:
                r.raise_for_status()
                total      = int(r.headers.get("Content-Length") or 0)
                downloaded = 0
                os.makedirs(output_dir, exist_ok=True)
                with open(str(dest), "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(downloaded, total)

            # 6. Validazione (preview da 30s, durata errata)
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, err_msg, self.name)

            # 7. MusicBrainz tags
            mb_tags: dict[str, str] = {}
            if mb_fetcher:
                mb_result = mb_fetcher.future.result()
                mb_tags   = mb_result_to_tags(mb_result)
                _print_mb_summary(mb_tags)

            # 8. Embed metadata (tagger pipeline standard)
            # Per MP3 usiamo il tagger nativo; per M4A anche
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
            embed_metadata(str(dest), metadata, opts, session=self._session)

            return DownloadResult.ok(self.name, str(dest), fmt=fmt_str)

        except SpotiflacError as exc:
            logger.error("[pandora] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[pandora] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")


# ---------------------------------------------------------------------------
# URL detection helper (usato da SpotiflacDownloader._resolve_metadata)
# ---------------------------------------------------------------------------

def is_pandora_url(url: str) -> bool:
    """Restituisce True se l'URL appartiene a Pandora."""
    return _is_pandora_url(url)


def parse_pandora_url(url: str) -> dict[str, str]:
    """
    Analizza un URL Pandora e restituisce {"type": ..., "id": ...}.
    Usato da SpotiflacDownloader per determinare il tipo di collezione.
    """
    try:
        input_url = url.strip()

        # App link: necessita risoluzione HTTP — restituiamo "track" come default
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
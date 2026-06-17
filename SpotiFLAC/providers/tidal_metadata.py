"""
TidalMetadataClient — retrieves metadata for tracks/albums/playlists/artists
from the public Tidal API when the input URL is a Tidal link (not Spotify).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

import httpx

from ..core.errors import AuthError, ErrorKind, InvalidUrlError, NetworkError, SpotiflacError
from ..core.http import NetworkManager
from ..core.models import TrackMetadata

logger = logging.getLogger(__name__)

# Configurazione sincronizzata con manifest.json
_TIDAL_CLIENT_ID   = "49YxDN9a2aFV6RTG"
_TIDAL_API_BASE    = "https://api.tidal.com/v1"
_TIDAL_COUNTRY     = "US"
_TIDAL_LOCALE      = "en_US"
_TIDAL_DEVICE_TYPE = "BROWSER"
_TIDAL_UA        = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_TIDAL_DOMAINS = {"listen.tidal.com", "tidal.com", "www.tidal.com"}

_PAGE_SIZE = 50  # Modificato a 50 in base a index.js pageSize

_TIDAL_FILTER_ALBUMS       = "ALBUMS"
_TIDAL_FILTER_EPSANDSINGLES = "EPSANDSINGLES"
_TIDAL_FILTER_COMPILATIONS  = "COMPILATIONS"


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def is_tidal_url(url: str) -> bool:
    """Returns True if the URL belongs to Tidal, including deep links."""
    url_lower = url.lower().strip()
    if url_lower.startswith("tidal:"):
        return True
    try:
        return urlparse(url).netloc in _TIDAL_DOMAINS
    except Exception:
        return False


def parse_tidal_url(url: str) -> dict[str, str]:
    """
    Parse a Tidal URL or deep link and return {"type": ..., "id": ...}.
    Synchronized with the parseURL logic from index.js.
    """
    text = url.strip()
    
    # 1. Prefisso puro (es. tidal:track:12345)
    prefix_match = re.match(r"^tidal:(track|album|artist|playlist):([^?#/]+)", text, re.IGNORECASE)
    if prefix_match:
        return {"type": prefix_match.group(1).lower(), "id": prefix_match.group(2)}

    # 2. Deep link (es. tidal://track/12345)
    deep_link_match = re.match(r"^tidal:\/\/\/?(track|album|artist|playlist)\/([^?#/]+)", text, re.IGNORECASE)
    if deep_link_match:
        return {"type": deep_link_match.group(1).lower(), "id": deep_link_match.group(2)}

    # 3. HTTPS URL
    normalized = text
    if not normalized.startswith("http://") and not normalized.startswith("https://"):
        normalized = "https://" + normalized

    u = urlparse(normalized)
    if u.netloc.lower() not in _TIDAL_DOMAINS:
        raise InvalidUrlError(url)

    path = u.path.strip("/")
    parts = path.split("/")
    
    if len(parts) > 0 and parts[0] == "browse":
        parts.pop(0)

    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist", "artist"):
        entity_type = parts[0]
        entity_id   = parts[1]

        if entity_type == "artist" and len(parts) >= 3 and parts[2] == "discography":
            group = parts[3] if len(parts) >= 4 else "all"
            return {"type": "artist_discography", "id": entity_id, "group": group}

        return {"type": entity_type, "id": entity_id}

    raise InvalidUrlError(url)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _remove_diacritics(s: str) -> str:
    """Removes accenti e caratteri speciali imitando removeDiacritics in index.js"""
    try:
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    except Exception:
        pass
    
    # Replacements speciali come nel JS
    s = re.sub(r"[đĐ]", "dj", s)
    s = re.sub(r"[ßẞ]", "ss", s)
    s = re.sub(r"[æÆ]", "ae", s)
    s = re.sub(r"[œŒ]", "oe", s)
    return s

def _normalize_artist(s: str) -> str:
    s = _remove_diacritics(s).lower()
    s = re.sub(r"[&]", " and ", s)
    s = re.sub(r"[^\w\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _artist_in_track(artist_name: str, track_artists: str) -> bool:
    name_norm = _normalize_artist(artist_name)
    for artist in track_artists.split(","):
        if _normalize_artist(artist) == name_norm:
            return True
    return False


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TidalMetadataClient:
    """
    Retrieves metadati dall'API pubblica di Tidal v1.
    """

    def __init__(self, timeout_s: int = 15) -> None:
        self._timeout = timeout_s
        self._session = NetworkManager.get_sync_client()
        self._session.headers.update({
            "X-Tidal-Token": _TIDAL_CLIENT_ID,
            "Accept":        "application/json",
            "User-Agent":    _TIDAL_UA,
        })

    def _get(self, path: str, extra_params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {
            "countryCode": _TIDAL_COUNTRY,
            "locale": _TIDAL_LOCALE,
            "deviceType": _TIDAL_DEVICE_TYPE
        }
        if extra_params:
            params.update(extra_params)

        url = f"{_TIDAL_API_BASE}/{path.lstrip('/')}"
        _MAX_RATE_LIMIT_RETRIES = 3

        for _attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)
            except httpx.RequestError as exc:
                if _attempt >= _MAX_RATE_LIMIT_RETRIES:
                    raise NetworkError("tidal_metadata", f"Network error on {path}: {exc}")
                time.sleep(2)
                continue

            if resp.status_code == 401:
                raise AuthError("tidal_metadata", "Tidal token invalid or expired")
            if resp.status_code == 404:
                raise SpotiflacError(
                    ErrorKind.TRACK_NOT_FOUND,
                    f"Resource not found: {path}",
                    "tidal_metadata",
                )
            if resp.status_code == 429:
                if _attempt >= _MAX_RATE_LIMIT_RETRIES:
                    raise NetworkError(
                        "tidal_metadata",
                        f"Rate limit persistente dopo {_MAX_RATE_LIMIT_RETRIES} tentativi su {path}",
                    )
                wait = int(resp.headers.get("Retry-After", 5)) + 1
                logger.warning(
                    "[tidal_metadata] Rate limited (attempt %d/%d) — waiting %ds",
                    _attempt + 1, _MAX_RATE_LIMIT_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise NetworkError("tidal_metadata", f"HTTP {resp.status_code} from {path}")

            return resp.json()

        raise NetworkError("tidal_metadata", f"Unable to complete request to {path}")

    def _pagete(self, path: str, extra_params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        items:  list[dict[str, Any]] = []
        offset: int = 0

        while True:
            params = {"limit": _PAGE_SIZE, "offset": offset}
            if extra_params:
                params.update(extra_params)

            data  = self._get(path, params)
            page  = data.get("items", [])
            total = data.get("totalNumberOfItems", len(page))

            items.extend(page)
            offset += len(page)

            logger.debug("[tidal_metadata] pagination %s: %d/%d", path, offset, total)

            if offset >= total or not page:
                break

            time.sleep(0.3)

        return items

    def get_track(self, track_id: str) -> TrackMetadata:
        data = self._get(f"/tracks/{track_id}")
        return self._track_from_raw(data)

    def get_album_tracks(
            self,
            album_id: str,
            preloaded_album: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        album = preloaded_album if preloaded_album else self._get(f"/albums/{album_id}")
        items = self._pagete(f"/albums/{album_id}/tracks")
        tracks = [self._track_from_album_item(item, album) for item in items]
        
        formatted_album = {
            "title": album.get("title", "Unknown"),
            "cover_url": self._cover_url(album),
            "releaseDate": album.get("releaseDate", ""),
        }
        return formatted_album, tracks

    def get_playlist_tracks(self, playlist_uuid: str) -> tuple[dict[str, Any], list[TrackMetadata]]:
        playlist  = self._get(f"/playlists/{playlist_uuid}")
        raw_items = self._pagete(f"/playlists/{playlist_uuid}/tracks")

        tracks: list[TrackMetadata] = []
        for entry in raw_items:
            track_data = entry.get("item") or entry
            if not track_data or not track_data.get("id"):
                continue

            if track_data.get("streamReady") is False:
                logger.debug(
                    "[tidal_metadata] track not available skipped: %s",
                    track_data.get("title", "?"),
                )
                continue

            tracks.append(self._track_from_raw(track_data, fetch_album_details=False))

        return playlist, tracks

    def get_artist_albums(
            self,
            artist_id: str,
            include_groups: str = f"{_TIDAL_FILTER_ALBUMS},{_TIDAL_FILTER_EPSANDSINGLES}",
            include_featuring: bool = False,
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        artist = self._get(f"/artists/{artist_id}")
        artist_name = artist.get("name", "")
        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()
        seen_album_ids: set[str] = set()

        albums_to_fetch: list[tuple[str, dict[str, Any], bool]] = []

        if include_featuring:
            existing = include_groups.split(",")
            if _TIDAL_FILTER_COMPILATIONS not in existing:
                existing.append(_TIDAL_FILTER_COMPILATIONS)
            include_groups = ",".join(existing)

        for group in include_groups.split(","):
            group = group.strip().upper()
            if not group:
                continue

            try:
                albums = self._pagete(
                    f"/artists/{artist_id}/albums",
                    extra_params={"filter": group},
                )
            except Exception as exc:
                logger.warning("[tidal_metadata] gruppo %s fallito: %s", group, exc)
                continue

            is_compilation = (group == _TIDAL_FILTER_COMPILATIONS)

            for album_data in albums:
                album_id = str(album_data.get("id", ""))
                if not album_id or album_id in seen_album_ids:
                    continue
                seen_album_ids.add(album_id)
                albums_to_fetch.append((album_id, album_data, is_compilation))

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_album = {
                executor.submit(self.get_album_tracks, aid, preloaded): (aid, is_comp)
                for aid, preloaded, is_comp in albums_to_fetch
            }

            results: dict[str, tuple[list[TrackMetadata], bool]] = {}
            for future in as_completed(future_to_album):
                album_id, is_compilation = future_to_album[future]
                try:
                    _, album_tracks = future.result()
                    results[album_id] = (album_tracks, is_compilation)
                except Exception as exc:
                    logger.warning("[tidal_metadata] album %s skipped: %s", album_id, exc)

        for album_id, _, is_compilation in albums_to_fetch:
            if album_id not in results:
                continue
            album_tracks, _ = results[album_id]
            for track in album_tracks:
                if track.isrc and track.isrc in seen_isrc:
                    continue
                if is_compilation and not _artist_in_track(artist_name, track.artists):
                    continue
                if track.isrc:
                    seen_isrc.add(track.isrc)
                tracks.append(track)
                
        return artist, tracks

    def get_url(self, tidal_url: str, include_featuring: bool = False) -> tuple[str, list[TrackMetadata], str, dict[str, Any]]:
        info = parse_tidal_url(tidal_url)
        t    = info["type"]

        if t == "track":
            meta = self.get_track(info["id"])
            return meta.title, [meta], meta.cover_url, {}

        if t == "album":
            album, tracks = self.get_album_tracks(info["id"])
            album_meta = {
                "release_date": album.get("releaseDate", ""),
                "track_count": len(tracks),
            }
            return album.get("title", "Unknown Album"), tracks, album.get("cover_url", ""), album_meta

        if t == "playlist":
            playlist, tracks = self.get_playlist_tracks(info["id"])
            return playlist.get("title", "Unknown Playlist"), tracks, playlist.get("cover_url", ""), {}

        if t in ("artist", "artist_discography"):
            group_map = {
                "albums":       _TIDAL_FILTER_ALBUMS,
                "eps":          _TIDAL_FILTER_EPSANDSINGLES,
                "singles":      _TIDAL_FILTER_EPSANDSINGLES,
                "compilations": _TIDAL_FILTER_COMPILATIONS,
                "all":          f"{_TIDAL_FILTER_ALBUMS},{_TIDAL_FILTER_EPSANDSINGLES},{_TIDAL_FILTER_COMPILATIONS}",
            }
            raw_group      = info.get("group", "all")
            include_groups = group_map.get(
                raw_group,
                f"{_TIDAL_FILTER_ALBUMS},{_TIDAL_FILTER_EPSANDSINGLES}",
            )
            artist, tracks = self.get_artist_albums(
                info["id"],
                include_groups,
                include_featuring=include_featuring,
            )
            return artist.get("name", "Unknown Artist"), tracks, artist.get("avatar", ""), {}

        raise SpotiflacError(
            ErrorKind.INVALID_URL,
            f"Tidal type not supported: {t} (supported: track, album, playlist, artist)",
        )

    @staticmethod
    def _format_artists(artists: list[dict[str, Any]] | None) -> str:
        if not artists:
            return "Unknown"
        return ", ".join(a.get("name", "Unknown") for a in artists if a.get("name"))

    @staticmethod
    def _cover_url(album: dict[str, Any]) -> str:
        cover = album.get("cover", "")
        if not cover:
            return ""
        return f"https://resources.tidal.com/images/{cover.replace('-', '/')}/1280x1280.jpg"

    def _fetch_album_details(self, album_id: int | str) -> dict[str, Any]:
        try:
            return self._get(f"/albums/{album_id}")
        except Exception as exc:
            logger.debug("[tidal_metadata] Unable to fetch album %s: %s", album_id, exc)
            return {}

    def _track_from_raw(
            self,
            data: dict[str, Any],
            fetch_album_details: bool = True,
    ) -> TrackMetadata:
        album   = data.get("album", {})
        artists = data.get("artists") or ([data["artist"]] if data.get("artist") else [])

        cover_url         = self._cover_url(album)
        release_date      = album.get("releaseDate", "")
        total_tracks      = album.get("numberOfTracks", 0)
        album_artists_raw = album.get("artists") or artists

        if fetch_album_details and album.get("id"):
            album_details = self._fetch_album_details(album["id"])
            if album_details:
                cover_url         = self._cover_url(album_details) or cover_url
                release_date      = album_details.get("releaseDate", release_date)
                total_tracks      = album_details.get("numberOfTracks", total_tracks)
                album_artists_raw = album_details.get("artists") or album_artists_raw

        # Garantisci fallback durata_ms se assente
        duration_ms = int(data.get("duration", 0)) * 1000

        return TrackMetadata(
            id           = f"tidal_{data.get('id', '')}",
            title        = data.get("title", "Unknown"),
            artists      = self._format_artists(artists),
            album        = album.get("title", "Unknown"),
            album_artist = self._format_artists(album_artists_raw),
            isrc         = data.get("isrc", ""),
            track_number = data.get("trackNumber", 0),
            disc_number  = data.get("volumeNumber", 1),
            total_tracks = total_tracks,
            duration_ms  = duration_ms,
            release_date = release_date,
            cover_url    = cover_url,
            external_url = data.get("url", ""),
        )

    def _track_from_album_item(self, data: dict[str, Any], album: dict[str, Any]) -> TrackMetadata:
        artists = data.get("artists") or ([data["artist"]] if data.get("artist") else [])

        return TrackMetadata(
            id           = f"tidal_{data.get('id', '')}",
            title        = data.get("title", "Unknown"),
            artists      = self._format_artists(artists),
            album        = album.get("title", "Unknown"),
            album_artist = self._format_artists(album.get("artists") or artists),
            isrc         = data.get("isrc", ""),
            track_number = data.get("trackNumber", 0),
            disc_number  = data.get("volumeNumber", 1),
            total_tracks = album.get("numberOfTracks", 0),
            duration_ms  = int(data.get("duration", 0)) * 1000,
            release_date = album.get("releaseDate", ""),
            cover_url    = self._cover_url(album),
            external_url = data.get("url", ""),
        )
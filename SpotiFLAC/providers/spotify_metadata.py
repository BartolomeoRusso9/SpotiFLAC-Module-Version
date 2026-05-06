"""
SpotifyMetadataProvider — refactored.
"""
from __future__ import annotations
import base64
import logging
import time
from typing import Iterator
from urllib.parse import urlparse, parse_qs

import requests

from ..core.errors import AuthError, NetworkError, InvalidUrlError, SpotiflacError, ErrorKind
from ..core.models import TrackMetadata
from ..core.isrc_cache import get_cached_isrc, put_cached_isrc
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

_CLIENT_ID     = base64.b64decode("ODNlNDQzMGI0NzAwNDM0YmFhMjEyMjhhOWM3ZDExYzU=").decode()
_CLIENT_SECRET = base64.b64decode("OWJiOWUxMzFmZjI4NDI0Y2I2YTQyMGFmZGY0MWQ0NGE=").decode()
_TOKEN_URL     = "https://accounts.spotify.com/api/token"
_API_BASE      = "https://api.spotify.com/v1"


def parse_spotify_url(uri: str) -> dict[str, str]:
    u = urlparse(uri)

    if u.netloc == "embed.spotify.com":
        qs = parse_qs(u.query)
        if not qs.get("uri"):
            raise InvalidUrlError(uri)
        return parse_spotify_url(qs["uri"][0])

    if u.scheme == "spotify":
        parts = uri.split(":")
    elif u.netloc in ("open.spotify.com", "play.spotify.com"):
        parts = u.path.split("/")
        if len(parts) > 1 and parts[1] == "embed":
            parts = parts[1:]
        if len(parts) > 1 and parts[1].startswith("intl-"):
            parts = parts[1:]
    elif not u.scheme and not u.netloc:
        return {"type": "playlist", "id": u.path}
    else:
        raise InvalidUrlError(uri)

    if len(parts) == 3 and parts[1] in ("album", "track", "playlist", "artist"):
        return {"type": parts[1], "id": parts[2].split("?")[0]}
    if len(parts) == 5 and parts[3] == "playlist":
        return {"type": "playlist", "id": parts[4].split("?")[0]}
    if len(parts) >= 4 and parts[1] == "artist":
        dtype = "artist_discography" if parts[3] == "discography" else "artist"
        return {"type": dtype, "id": parts[2].split("?")[0]}

    raise InvalidUrlError(uri)


class SpotifyMetadataClient:
    def __init__(self, timeout_s: int = 10) -> None:
        self._timeout    = timeout_s
        self._session    = requests.Session()
        self._token      = ""
        self._token_exp  = 0.0

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token

        auth = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
        resp = self._session.post(
            _TOKEN_URL,
            headers = {"Authorization": f"Basic {auth}",
                       "Content-Type": "application/x-www-form-urlencoded"},
            data    = {"grant_type": "client_credentials"},
            timeout = self._timeout,
        )
        if resp.status_code != 200:
            raise AuthError("spotify", f"Token request failed: HTTP {resp.status_code}")

        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthError("spotify", "No access_token in token response")

        self._token     = token
        self._token_exp = time.time() + body.get("expires_in", 3600)
        return self._token

    def _get(self, path: str, **kwargs) -> dict:
        token = self._ensure_token()
        resp  = self._session.get(
            f"{_API_BASE}/{path.lstrip('/')}",
            headers = {"Authorization": f"Bearer {token}"},
            timeout = self._timeout,
            **kwargs,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5)) + 1
            logger.info("[spotify] Rate limited — sleeping %ss", retry_after)
            time.sleep(retry_after)
            return self._get(path, **kwargs)
        if resp.status_code != 200:
            raise NetworkError("spotify", f"HTTP {resp.status_code} from {path}")
        return resp.json()

    def _paginate(self, url: str, delay: float = 0.5) -> Iterator[dict]:
        while url:
            data  = self._get(url.replace(f"{_API_BASE}/", ""))
            items = data.get("items", [])
            yield from items
            url = (data.get("next") or "").split("&locale=")[0] or ""
            if url and delay > 0:
                time.sleep(delay)

    def get_track(self, track_id: str) -> TrackMetadata:
        data = self._get(f"/tracks/{track_id}")
        return self._track_from_raw(data)

    def get_album_tracks(self, album_id: str) -> tuple[dict, list[TrackMetadata]]:
        album = self._get(f"/albums/{album_id}")
        raw_items = list(self._paginate(f"{_API_BASE}/albums/{album_id}/tracks?limit=50"))

        missing_isrc_ids = []
        isrc_map = {}

        # 1. Controlliamo la cache prima di fare qualsiasi richiesta
        for item in raw_items:
            track_id = item["id"]
            cached = get_cached_isrc(track_id)
            if cached:
                isrc_map[track_id] = cached
            else:
                missing_isrc_ids.append(track_id)

        # 2. Recuperiamo gli ISRC mancanti a blocchi di 50 (limite API Spotify)
        for i in range(0, len(missing_isrc_ids), 50):
            chunk = missing_isrc_ids[i:i+50]
            try:
                ids_str = ",".join(chunk)
                # Endpoint batch di Spotify
                data = self._get(f"/tracks?ids={ids_str}")
                for full_track in data.get("tracks", []):
                    if full_track:
                        tid = full_track["id"]
                        tisrc = full_track.get("external_ids", {}).get("isrc", "")
                        isrc_map[tid] = tisrc
                        if tisrc:
                            put_cached_isrc(tid, tisrc)
            except Exception as exc:
                logger.warning("[spotify] Fallimento nel recupero batch degli ISRC: %s", exc)

        # 3. Costruiamo gli oggetti TrackMetadata
        tracks: list[TrackMetadata] = []
        for item in raw_items:
            track_id = item["id"]
            isrc = isrc_map.get(track_id, "")
            tracks.append(self._track_from_album_item(item, album, isrc))

        return album, tracks

    def get_playlist_tracks(self, playlist_id: str) -> tuple[dict, list[TrackMetadata]]:
        playlist = self._get(f"/playlists/{playlist_id}")
        tracks: list[TrackMetadata] = []

        for item in self._paginate(f"{_API_BASE}/playlists/{playlist_id}/tracks?limit=100"):
            track = item.get("track")
            if not track or not track.get("id"):
                continue
            tracks.append(self._track_from_raw(track))

        return playlist, tracks

    def get_artist_albums(
            self,
            artist_id: str,
            include_groups: str = "album,single",
    ) -> tuple[dict, list[TrackMetadata]]:

        artist = self._get(f"/artists/{artist_id}")
        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()
        seen_album_ids: set[str] = set()

        # Raccogliamo tutti gli ID degli album prima
        albums_to_fetch = []
        for item in self._paginate(
                f"{_API_BASE}/artists/{artist_id}/albums"
                f"?include_groups={include_groups}&limit=50"
        ):
            album_id = item.get("id")
            if album_id and album_id not in seen_album_ids:
                seen_album_ids.add(album_id)
                albums_to_fetch.append(album_id)

        # Parallelizziamo le richieste degli album (max 5 worker per non arrabbiare le API)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=5) as executor:
            # Sottomettiamo tutti i task
            future_to_album = {
                executor.submit(self.get_album_tracks, aid): aid for aid in albums_to_fetch
            }

            for future in as_completed(future_to_album):
                album_id = future_to_album[future]
                try:
                    _, album_tracks = future.result()
                    for track in album_tracks:
                        # Logica di deduplicazione
                        if track.isrc and track.isrc in seen_isrc:
                            continue
                        if track.isrc:
                            seen_isrc.add(track.isrc)
                        tracks.append(track)
                except Exception as exc:
                    logger.warning("[spotify] album %s saltato: %s", album_id, exc)

        return artist, tracks

    def get_url(self, spotify_url: str) -> tuple[str, list[TrackMetadata]]:
        info = parse_spotify_url(spotify_url)
        t    = info["type"]

        if t == "track":
            meta = self.get_track(info["id"])
            return meta.title, [meta]

        if t == "album":
            album, tracks = self.get_album_tracks(info["id"])
            name = album.get("name", "Unknown Album")
            return name, tracks

        if t == "playlist":
            pl, tracks = self.get_playlist_tracks(info["id"])
            name = pl.get("name", "Unknown Playlist")
            return name, tracks

        if t in ("artist", "artist_discography"):
            artist, tracks = self.get_artist_albums(info["id"])
            return artist.get("name", "Unknown Artist"), tracks

        raise SpotiflacError(
            ErrorKind.INVALID_URL,
            f"Unsupported Spotify URL type: {t}",
        )

    @staticmethod
    def _format_artists(artists: list[dict] | str) -> str:
        if isinstance(artists, str):
            return artists
        return ", ".join(
            a.get("name", "Unknown") if isinstance(a, dict) else str(a)
            for a in artists
        )

    @staticmethod
    def _best_image(images: list[dict]) -> str:
        return images[0].get("url", "") if images else ""

    def _track_from_raw(self, data: dict) -> TrackMetadata:
        album       = data.get("album", {})
        artists     = self._format_artists(data.get("artists", []))
        album_artists = self._format_artists(album.get("artists", []) or data.get("artists", []))
        cover       = self._best_image(
            album.get("images") or data.get("images", [])
        )
        copyrights = album.get("copyrights", [])
        copyright_text = copyrights[0].get("text", "") if copyrights else ""
        return TrackMetadata(
            id           = data.get("id", ""),
            title        = data.get("name", "Unknown"),
            artists      = artists,
            album        = album.get("name", data.get("album_name", "Unknown")),
            album_artist = album_artists,
            isrc         = data.get("external_ids", {}).get("isrc", ""),
            track_number = data.get("track_number", 0),
            disc_number  = data.get("disc_number", 1),
            total_tracks = album.get("total_tracks", 0),
            duration_ms  = data.get("duration_ms", 0),
            release_date = album.get("release_date", ""),
            cover_url    = cover,
            external_url = data.get("external_urls", {}).get("spotify", ""),
            copyright    = copyright_text,
            composer     = ""
        )

    def _track_from_album_item(
            self,
            item:  dict,
            album: dict,
            isrc:  str,
    ) -> TrackMetadata:
        artists       = self._format_artists(item.get("artists", []))
        album_artists = self._format_artists(album.get("artists", []))
        cover         = self._best_image(album.get("images", []))
        copyrights = album.get("copyrights", [])
        copyright_text = copyrights[0].get("text", "") if copyrights else ""

        return TrackMetadata(
            id           = item.get("id", ""),
            title        = item.get("name", "Unknown"),
            artists      = artists,
            album        = album.get("name", "Unknown"),
            album_artist = album_artists,
            isrc         = isrc,
            track_number = item.get("track_number", 0),
            disc_number  = item.get("disc_number", 1),
            total_tracks = album.get("total_tracks", 0),
            duration_ms  = item.get("duration_ms", 0),
            release_date = album.get("release_date", ""),
            cover_url    = cover,
            external_url = item.get("external_urls", {}).get("spotify", ""),
            copyright    = copyright_text,
            composer     = ""
        )
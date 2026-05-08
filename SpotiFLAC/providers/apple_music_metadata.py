"""
AppleMusicMetadataClient — recupera metadati di tracce/album/artisti/playlist
tramite la AMP API pubblica di Apple Music.
"""
from __future__ import annotations

import logging
import re
import json
import urllib.parse
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from ..core.errors import NetworkError, SpotiflacError, ErrorKind, InvalidUrlError
from ..core.models import TrackMetadata

logger = logging.getLogger(__name__)

_APPLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def is_apple_music_url(url: str) -> bool:
    """Restituisce True se l'URL appartiene a Apple Music."""
    return "music.apple.com" in url.lower() or "apple.com" in url.lower()

def parse_apple_music_url(url: str) -> dict[str, str]:
    u = urlparse(url)

    # Check traccia via query param
    song_id_match = re.search(r"[?&]i=(\d+)", url)

    path = u.path.strip("/")
    parts = [p for p in path.split("/") if p]

    # Estrazione dinamica dello storefront (default 'us')
    storefront = "us"
    if len(parts) > 0 and len(parts[0]) == 2:
        storefront = parts[0]

    # Se c'è 'i=', è sicuramente una traccia, ma ci serve anche lo storefront
    if song_id_match:
        return {"type": "track", "id": song_id_match.group(1), "storefront": storefront}

    if "song" in parts:
        idx = parts.index("song")
        return {"type": "track", "id": parts[idx + 2] if len(parts) > idx + 2 else parts[idx + 1], "storefront": storefront}

    if "album" in parts:
        idx = parts.index("album")
        return {"type": "album", "id": parts[idx + 2] if len(parts) > idx + 2 else parts[idx + 1], "storefront": storefront}

    if "playlist" in parts:
        idx = parts.index("playlist")
        return {"type": "playlist", "id": parts[idx + 2] if len(parts) > idx + 2 else parts[idx + 1], "storefront": storefront}

    if "artist" in parts:
        idx = parts.index("artist")
        return {"type": "artist", "id": parts[idx + 2] if len(parts) > idx + 2 else parts[idx + 1], "storefront": storefront}

    raise InvalidUrlError(url)

# ---------------------------------------------------------------------------
# Helper Normalizzazione
# ---------------------------------------------------------------------------

def _normalize_artist(s: str) -> str:
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", "", s)
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

class AppleMusicMetadataClient:
    def __init__(self, timeout_s: int = 15) -> None:
        self._timeout = timeout_s
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _APPLE_UA,
            "Accept": "application/json",
            "Origin": "https://music.apple.com"
        })
        self._auth_token = None

    def _get_token(self) -> str:
        """Estrae il token JWT anonimo dal frontend web."""
        if self._auth_token: return self._auth_token
        try:
            res = self._session.get("https://music.apple.com/us/browse", timeout=self._timeout)
            meta_match = re.search(r'<meta\s+name="desktop-music-app/config/environment"\s+content="([^"]+)"', res.text)
            if meta_match:
                data = json.loads(urllib.parse.unquote(meta_match.group(1)))
                self._auth_token = data.get("MEDIA_API", {}).get("token")
                return self._auth_token
        except Exception as e:
            logger.warning("[apple_metadata] Impossibile recuperare JWT token: %s", e)
        return ""

    def _get(self, path: str, params: dict | None = None) -> dict:
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        resp = self._session.get(
            f"https://amp-api.music.apple.com/v1/catalog/{path.lstrip('/')}",
            params=params,
            headers=headers,
            timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Metodi di Fetching
    # ------------------------------------------------------------------

    def get_track(self, track_id: str, storefront: str = "us") -> TrackMetadata:
        data = self._get(f"/{storefront}/songs/{track_id}", {"include": "albums"})
        results = data.get("data", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Traccia {track_id} non trovata.")
        return self._parse_item(results[0])

    def get_album_tracks(self, album_id: str, storefront: str = "us") -> tuple[dict, list[TrackMetadata]]:
        data = self._get(f"/{storefront}/albums/{album_id}", {"include": "tracks"})
        results = data.get("data", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Album {album_id} non trovato.")

        album_data = results[0]
        tracks_data = album_data.get("relationships", {}).get("tracks", {}).get("data", [])
        tracks = [self._parse_item(item, album_data) for item in tracks_data]
        return album_data, tracks

    def get_playlist_tracks(self, playlist_id: str, storefront: str = "us") -> tuple[dict, list[TrackMetadata]]:
        data = self._get(f"/{storefront}/playlists/{playlist_id}", {"include": "tracks"})
        results = data.get("data", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Playlist {playlist_id} non trovata.")

        playlist_data = results[0]
        # Potresti implementare la paginazione chiamando 'next' in playlist_data.relationships.tracks in futuro
        tracks_data = playlist_data.get("relationships", {}).get("tracks", {}).get("data", [])
        tracks = [self._parse_item(item) for item in tracks_data if item.get("type") == "songs"]
        return playlist_data, tracks

    def get_artist_albums(
            self,
            artist_id: str,
            include_featuring: bool = False,
            storefront: str = "us"
    ) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera la discografia completa di un artista Apple Music via AMP API.
        """
        # Fetch dati artista includendo gli album associati
        data = self._get(f"/{storefront}/artists/{artist_id}", {"include": "albums"})
        results = data.get("data", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Artista {artist_id} non trovato.")

        artist_data = results[0]
        artist_name = artist_data.get("attributes", {}).get("name", "Unknown")

        # Raccogliamo gli ID degli album dalle relazioni (relationships)
        albums_data = artist_data.get("relationships", {}).get("albums", {}).get("data", [])
        albums_to_fetch = [str(a.get("id")) for a in albums_data if a.get("id")]

        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()

        # Fetch parallelo dei metadati (max 5 richieste simultanee per rispettare rate limit)
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_album = {
                executor.submit(self.get_album_tracks, aid, storefront=storefront): aid
                for aid in albums_to_fetch
            }

            results_dict = {}
            for future in as_completed(future_to_album):
                aid = future_to_album[future]
                try:
                    _, album_tracks = future.result()
                    results_dict[aid] = album_tracks
                except Exception as exc:
                    logger.warning("[apple_metadata] album %s saltato: %s", aid, exc)

        # Ricostruiamo la lista di tracce
        for aid in albums_to_fetch:
            if aid not in results_dict:
                continue
            for track in results_dict[aid]:
                if track.isrc and track.isrc in seen_isrc:
                    logger.debug("[apple_metadata] duplicato saltato: %s (ISRC %s)", track.title, track.isrc)
                    continue

                if not include_featuring and not _artist_in_track(artist_name, track.artists):
                    continue

                if track.isrc:
                    seen_isrc.add(track.isrc)
                tracks.append(track)

        return artist_data, tracks

    # ------------------------------------------------------------------
    # Entry point pubblico
    # ------------------------------------------------------------------

    def get_url(self, url: str, include_featuring: bool = False) -> tuple[str, list[TrackMetadata]]:
        info = parse_apple_music_url(url)
        t = info["type"]
        storefront = info.get("storefront", "us") # Estrae lo storefront per propagarlo

        if t == "track":
            meta = self.get_track(info["id"], storefront=storefront)
            return meta.title, [meta]

        if t == "album":
            album, tracks = self.get_album_tracks(info["id"], storefront=storefront)
            name = album.get("attributes", {}).get("name", "Unknown Album")
            return name, tracks

        if t == "playlist":
            playlist, tracks = self.get_playlist_tracks(info["id"], storefront=storefront)
            name = playlist.get("attributes", {}).get("name", "Unknown Playlist")
            return name, tracks

        if t == "artist":
            artist, tracks = self.get_artist_albums(
                info["id"],
                include_featuring=include_featuring,
                storefront=storefront
            )
            name = artist.get("attributes", {}).get("name", "Unknown Artist")
            return name, tracks

        raise SpotiflacError(
            ErrorKind.INVALID_URL,
            f"Tipo Apple Music non supportato: {t} (supportati: track, album, playlist, artist)"
        )

    # ------------------------------------------------------------------
    # Conversione dati API → TrackMetadata
    # ------------------------------------------------------------------

    def _parse_item(self, item: dict, parent_album: dict = None) -> TrackMetadata:
        attr = item.get("attributes", {})
        album_attr = parent_album.get("attributes", {}) if parent_album else {}

        # Artwork template replacement (es. {w}x{h}bb.jpg -> 3000x3000bb.jpg)
        artwork_dict = attr.get("artwork", {})
        cover_url = artwork_dict.get("url", "").replace("{w}x{h}", "3000x3000")
        if not cover_url and parent_album: # Fallback su copertina album
            cover_url = album_attr.get("artwork", {}).get("url", "").replace("{w}x{h}", "3000x3000")

        release_date = attr.get("releaseDate", "").split("T")[0]

        return TrackMetadata(
            id           = f"apple_{item.get('id', '')}",
            title        = attr.get("name", "Unknown"),
            artists      = attr.get("artistName", "Unknown"),
            album        = attr.get("albumName", album_attr.get("name", "Unknown")),
            album_artist = album_attr.get("artistName", attr.get("artistName", "Unknown")),
            isrc         = attr.get("isrc", ""),
            track_number = attr.get("trackNumber", 1),
            disc_number  = attr.get("discNumber", 1),
            duration_ms  = attr.get("durationInMillis", 0),
            release_date = release_date,
            cover_url    = cover_url,
            external_url = attr.get("url", ""),
            copyright    = album_attr.get("copyright", ""), # Ora recuperabile dall'album
            composer     = attr.get("composerName", "")     # Fornito nativamente da AMP
        )
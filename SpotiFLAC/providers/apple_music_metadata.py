import logging
import re
import requests
from typing import Any
from ..core.models import TrackMetadata
from ..core.errors import SpotiflacError, ErrorKind

logger = logging.getLogger(__name__)

def is_apple_music_url(url: str) -> bool:
    return "music.apple.com" in url.lower()

def parse_apple_music_url(url: str) -> dict[str, str] | None:
    """Parser robusto basato sui pattern ufficiali di Apple Music."""
    # Pattern: /album/nome-album/ID o /album/ID
    album_match = re.search(r"/album/.*/(\d+)", url)
    # Pattern: /artist/nome-artista/ID o /artist/ID
    artist_match = re.search(r"/artist/.*/(\d+)", url)
    # Pattern: /song/nome-canzone/ID o parametro ?i=ID
    song_id_match = re.search(r"[?&]i=(\d+)", url) or re.search(r"/song/.*/(\d+)", url)

    if song_id_match:
        return {"type": "track", "id": song_id_match.group(1)}
    if album_match:
        return {"type": "album", "id": album_match.group(1)}
    if artist_match:
        return {"type": "artist", "id": artist_match.group(1)}

    return None

class AppleMusicMetadataClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })

    def get_url(self, url: str, **kwargs) -> tuple[str, list[TrackMetadata]]:
        info = parse_apple_music_url(url)
        if not info:
            raise SpotiflacError(ErrorKind.INVALID_URL, f"URL Apple Music non riconosciuto: {url}")

        if info["type"] == "track":
            return self._get_track(info["id"])
        elif info["type"] == "album":
            return self._get_album(info["id"])
        elif info["type"] == "artist":
            return self._get_artist(info["id"])
        else:
            raise SpotiflacError(ErrorKind.UNSUPPORTED_FEATURE, f"Tipo {info['type']} non supportato.")

    def _get_track(self, track_id: str) -> tuple[str, list[TrackMetadata]]:
        url = f"https://itunes.apple.com/lookup?id={track_id}"
        resp = self._session.get(url, timeout=10)
        data = resp.json()
        if not data.get("results"):
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, "Traccia non trovata.")
        track = self._parse_item(data["results"][0])
        return track.title, [track]

    def _get_album(self, album_id: str) -> tuple[str, list[TrackMetadata]]:
        # Utilizziamo entity=song per ottenere tutte le tracce dell'album
        url = f"https://itunes.apple.com/lookup?id={album_id}&entity=song"
        resp = self._session.get(url, timeout=15)
        data = resp.json()
        results = data.get("results", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, "Album non trovato.")

        album_name = results[0].get("collectionName", "Unknown Album")
        tracks = [self._parse_item(item) for item in results if item.get("wrapperType") == "track"]
        return album_name, tracks

    def _get_artist(self, artist_id: str) -> tuple[str, list[TrackMetadata]]:
        # Recupera le top songs dell'artista (limite iTunes API è 50 per entity)
        url = f"https://itunes.apple.com/lookup?id={artist_id}&entity=song&limit=50"
        resp = self._session.get(url, timeout=15)
        data = resp.json()
        results = data.get("results", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, "Artista non trovato.")

        artist_name = results[0].get("artistName", "Unknown Artist")
        tracks = [self._parse_item(item) for item in results if item.get("wrapperType") == "track"]
        return f"Top Songs: {artist_name}", tracks

    def _parse_item(self, item: dict[str, Any]) -> TrackMetadata:
        cover_url = item.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")
        return TrackMetadata(
            id           = f"apple_{item.get('trackId', '')}",
            title        = item.get("trackName", "Unknown"),
            artists      = [item.get("artistName", "Unknown")],
            album        = item.get("collectionName", "Unknown"),
            album_artist = [item.get("artistName", "Unknown")],
            isrc         = item.get("isrc", ""),
            track_number = item.get("trackNumber", 1),
            duration_ms  = item.get("trackTimeMillis", 0),
            release_date = item.get("releaseDate", "").split("T")[0],
            cover_url    = cover_url,
            external_url = item.get("trackViewUrl", "")
        )
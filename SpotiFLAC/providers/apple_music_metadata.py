"""
AppleMusicMetadataClient — recupera metadati di tracce/album direttamente
dall'API pubblica di iTunes/Apple Music quando l'URL di input è un link Apple.
"""
from __future__ import annotations

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
    """Riconosce se il link è una traccia, un album o una playlist."""
    # Esempio traccia: music.apple.com/us/album/nome/123456?i=987654
    if "?i=" in url:
        track_id = url.split("?i=")[1].split("&")[0]
        return {"type": "track", "id": track_id}

    # Esempio album: music.apple.com/us/album/nome/123456
    if "/album/" in url:
        album_id = url.split("/")[-1].split("?")[0]
        return {"type": "album", "id": album_id}

    if "/playlist/" in url:
        playlist_id = url.split("/")[-1].split("?")[0]
        return {"type": "playlist", "id": playlist_id}

    if "/artist/" in url:
        artist_id = url.split("/")[-1].split("?")[0]
        return {"type": "artist", "id": artist_id}

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
            raise SpotiflacError(ErrorKind.INVALID_URL, f"URL Apple Music non valido o non riconosciuto: {url}")

        if info["type"] == "track":
            return self._get_track(info["id"])
        elif info["type"] == "album":
            return self._get_album(info["id"])
        else:
            raise SpotiflacError(
                ErrorKind.UNSUPPORTED_FEATURE,
                f"L'estrazione nativa per '{info['type']}' di Apple Music non è supportata via iTunes API. Usa un link Album o Track."
            )

    def _get_track(self, track_id: str) -> tuple[str, list[TrackMetadata]]:
        url = f"https://itunes.apple.com/lookup?id={track_id}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("resultCount", 0) == 0:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Traccia Apple Music {track_id} non trovata.")

        item = data["results"][0]
        track = self._parse_item(item)
        return track.title, [track]

    def _get_album(self, album_id: str) -> tuple[str, list[TrackMetadata]]:
        url = f"https://itunes.apple.com/lookup?id={album_id}&entity=song"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Album Apple Music {album_id} non trovato.")

        collection_info = results[0]
        collection_name = collection_info.get("collectionName", "Unknown Album")

        tracks = []
        for item in results[1:]:
            if item.get("wrapperType") == "track":
                tracks.append(self._parse_item(item))

        return collection_name, tracks

    def _parse_item(self, item: dict[str, Any]) -> TrackMetadata:
        """Converte i dati grezzi di iTunes in TrackMetadata."""
        # Trasforma l'immagine 100x100 di default in 1000x1000 ad alta risoluzione
        cover_url = item.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")

        release_date = item.get("releaseDate", "")
        if release_date:
            release_date = release_date.split("T")[0] # Prende solo YYYY-MM-DD

        return TrackMetadata(
            id           = f"apple_{item.get('trackId', '')}",
            title        = item.get("trackName", "Unknown"),
            artists      = [item.get("artistName", "Unknown")],
            album        = item.get("collectionName", "Unknown"),
            album_artist = [item.get("artistName", "Unknown")],
            isrc         = item.get("isrc", ""),
            track_number = item.get("trackNumber", 1),
            disc_number  = item.get("discNumber", 1),
            total_tracks = item.get("trackCount", 0),
            duration_ms  = item.get("trackTimeMillis", 0),
            release_date = release_date,
            cover_url    = cover_url,
            external_url = item.get("trackViewUrl", "")
        )
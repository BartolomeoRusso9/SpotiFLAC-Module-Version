"""
AppleMusicMetadataClient — recupera metadati di tracce/album/artisti
tramite l'API pubblica di iTunes Search / Lookup.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from ..core.errors import NetworkError, SpotiflacError, ErrorKind, InvalidUrlError
from ..core.models import TrackMetadata

logger = logging.getLogger(__name__)

_ITUNES_API_BASE = "https://itunes.apple.com"
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
    """
    Analizza un URL Apple Music e restituisce {"type": ..., "id": ...}.

    Esempi:
    - https://music.apple.com/us/album/name/123456789?i=987654321 -> track: 987654321
    - https://music.apple.com/us/album/name/123456789 -> album: 123456789
    - https://music.apple.com/us/playlist/name/pl.u-123456789 -> playlist: pl.u-123456789
    - https://music.apple.com/us/artist/name/123456789 -> artist: 123456789
    -  https://music.apple.com/it/song/name/123456789 -> song: 123456789
    """
    u = urlparse(url)

    # 1. Controllo per tracce (identificate dal parametro query 'i=')
    song_id_match = re.search(r"[?&]i=(\d+)", url)
    if song_id_match:
        return {"type": "track", "id": song_id_match.group(1)}

    # 2. Esplorazione del path per identificare song, album, playlist e artisti
    path = u.path.strip("/")
    parts = [p for p in path.split("/") if p]

    if "song" in parts:
        idx = parts.index("song")
        if len(parts) > idx + 2:
            return {"type": "track", "id": parts[idx + 2]}
        elif len(parts) > idx + 1 and parts[idx + 1].isdigit():
            return {"type": "track", "id": parts[idx + 1]}

    if "album" in parts:
        idx = parts.index("album")
        if len(parts) > idx + 2:
            return {"type": "album", "id": parts[idx + 2]}
        elif len(parts) > idx + 1 and parts[idx + 1].isdigit():
            return {"type": "album", "id": parts[idx + 1]}

    if "playlist" in parts:
        idx = parts.index("playlist")
        if len(parts) > idx + 2:
            return {"type": "playlist", "id": parts[idx + 2]}
        elif len(parts) > idx + 1:
            return {"type": "playlist", "id": parts[idx + 1]}

    if "artist" in parts:
        idx = parts.index("artist")
        if len(parts) > idx + 2:
            return {"type": "artist", "id": parts[idx + 2]}
        elif len(parts) > idx + 1:
            return {"type": "artist", "id": parts[idx + 1]}

    # Fallback con regex generiche
    song_match = re.search(r"/song/[^/]+/(\d+)", url) or re.search(r"/song/(\d+)", url)
    if song_match:
        return {"type": "track", "id": song_match.group(1)}

    album_match = re.search(r"/album/[^/]+/(\d+)", url) or re.search(r"/album/(\d+)", url)
    if album_match:
        return {"type": "album", "id": album_match.group(1)}

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
    """
    Recupera metadati tramite API iTunes Lookup.
    """

    def __init__(self, timeout_s: int = 15) -> None:
        self._timeout = timeout_s
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _APPLE_UA,
            "Accept": "application/json"
        })

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._session.get(
            f"{_ITUNES_API_BASE}/{path.lstrip('/')}",
            params=params,
            timeout=self._timeout
        )
        if resp.status_code in (403, 429):
            wait = int(resp.headers.get("Retry-After", 5)) + 1
            logger.warning("[apple_metadata] Rate limited — attendo %ds", wait)
            time.sleep(wait)
            return self._get(path, params)
        if resp.status_code != 200:
            raise NetworkError("apple_metadata", f"HTTP {resp.status_code} da {path}")
        return resp.json()

    # ------------------------------------------------------------------
    # Metodi di Fetching
    # ------------------------------------------------------------------

    def get_track(self, track_id: str) -> TrackMetadata:
        data = self._get("/lookup", {"id": track_id})
        results = data.get("results", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Traccia {track_id} non trovata.")
        return self._parse_item(results[0])

    def get_album_tracks(self, album_id: str, preloaded_album: dict | None = None) -> tuple[dict, list[TrackMetadata]]:
        data = self._get("/lookup", {"id": album_id, "entity": "song"})
        results = data.get("results", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Album {album_id} non trovato.")

        album_data = results[0] if results[0].get("wrapperType") == "collection" else (preloaded_album or {})
        tracks = [self._parse_item(item) for item in results if item.get("wrapperType") == "track"]

        return album_data, tracks

    def get_playlist_tracks(self, playlist_id: str) -> tuple[dict, list[TrackMetadata]]:
        # Le playlist di Apple Music richiedono MusicKit. L'API di iTunes Lookup pubblica non le supporta.
        raise SpotiflacError(
            ErrorKind.UNSUPPORTED_FEATURE,
            "L'estrazione dei metadati di intere playlist da Apple Music non è supportata "
            "tramite API pubblica iTunes. Utilizza l'URL di un album, traccia o artista."
        )

    def get_artist_albums(
            self,
            artist_id: str,
            include_featuring: bool = False
    ) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera la discografia completa di un artista Apple Music via API pubblica.
        """
        data = self._get("/lookup", {"id": artist_id, "entity": "album"})
        results = data.get("results", [])
        if not results:
            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, f"Artista {artist_id} non trovato.")

        artist_data = results[0]
        artist_name = artist_data.get("artistName", "")

        # Raccogliamo l'ID e i dettagli di tutti gli album
        albums_to_fetch = []
        for item in results[1:]:
            if item.get("wrapperType") == "collection":
                albums_to_fetch.append((str(item.get("collectionId")), item))

        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()

        # Fetch parallelo dei metadati (max 5 richieste simultanee per rispettare rate limit)
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_album = {
                executor.submit(self.get_album_tracks, aid, preloaded): aid
                for aid, preloaded in albums_to_fetch
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
        for aid, _ in albums_to_fetch:
            if aid not in results_dict:
                continue
            for track in results_dict[aid]:
                if track.isrc and track.isrc in seen_isrc:
                    logger.debug("[apple_metadata] duplicato saltato: %s (ISRC %s)", track.title, track.isrc)
                    continue

                # Su iTunes Search API le release sono miste (manca l'esatto concetto di compilation/appears_on di Spotify).
                # Filtrataggio logico basato sulla presenza del nome dell'artista.
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

        if t == "track":
            meta = self.get_track(info["id"])
            return meta.title, [meta]

        if t == "album":
            album, tracks = self.get_album_tracks(info["id"])
            name = album.get("collectionName", "Unknown Album")
            return name, tracks

        if t == "playlist":
            playlist, tracks = self.get_playlist_tracks(info["id"])
            return playlist.get("name", "Unknown Playlist"), tracks

        if t == "artist":
            artist, tracks = self.get_artist_albums(info["id"], include_featuring=include_featuring)
            return artist.get("artistName", "Unknown Artist"), tracks

        raise SpotiflacError(
            ErrorKind.INVALID_URL,
            f"Tipo Apple Music non supportato: {t} (supportati: track, album, artist)"
        )

    # ------------------------------------------------------------------
    # Conversione dati API → TrackMetadata
    # ------------------------------------------------------------------

    def _parse_item(self, item: dict) -> TrackMetadata:
        cover_url = item.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")
        release_date = item.get("releaseDate", "").split("T")[0] if item.get("releaseDate") else ""
        artist_name = item.get("artistName", "Unknown")

        return TrackMetadata(
            id           = f"apple_{item.get('trackId', '')}",
            title        = item.get("trackName", "Unknown"),
            artists      = artist_name,  # Spesso in iTunes Search è un'unica stringa comma-separated
            album        = item.get("collectionName", "Unknown"),
            album_artist = artist_name,
            isrc         = item.get("isrc", ""),
            track_number = item.get("trackNumber", 1),
            disc_number  = item.get("discNumber", 1),
            total_tracks = item.get("trackCount", 0),
            duration_ms  = item.get("trackTimeMillis", 0),
            release_date = release_date,
            cover_url    = cover_url,
            external_url = item.get("trackViewUrl", ""),
            copyright    = item.get("copyright", ""),
            composer     = ""
        )
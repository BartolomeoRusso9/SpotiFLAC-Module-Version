# SpotiFLAC/core/lyrics.py
"""
Fetch testi da LRCLib — port di lyrics.go.
"""
from __future__ import annotations
import logging
import urllib.parse
import requests

logger = logging.getLogger(__name__)

_LRCLIB = "https://lrclib.net/api"


def fetch_lyrics(
        track_name:  str,
        artist_name: str,
        album_name:  str = "",
        duration_s:  int = 0,
) -> str:
    """
    Cerca testi sincronizzati su LRCLib.
    Ritorna stringa LRC o testo plain, oppure "" se non trovato.
    """
    # Tentativo 1: ricerca esatta con album e durata
    result = _fetch_exact(track_name, artist_name, album_name, duration_s)
    if result:
        return result

    # Tentativo 2: senza album
    if album_name:
        result = _fetch_exact(track_name, artist_name, "", duration_s)
        if result:
            return result

    # Tentativo 3: ricerca testuale
    return _fetch_search(track_name, artist_name)


def _fetch_exact(track: str, artist: str, album: str, duration: int) -> str:
    params = {
        "artist_name": artist,
        "track_name":  track,
    }
    if album:
        params["album_name"] = album
    if duration > 0:
        params["duration"] = duration

    try:
        resp = requests.get(f"{_LRCLIB}/get", params=params, timeout=10)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        return data.get("syncedLyrics") or data.get("plainLyrics") or ""
    except Exception:
        return ""


def _fetch_search(track: str, artist: str) -> str:
    try:
        resp = requests.get(
            f"{_LRCLIB}/search",
            params={"artist_name": artist, "track_name": track},
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        results = resp.json()
        if not results:
            return ""
        # Preferisci synced
        for item in results:
            if item.get("syncedLyrics"):
                return item["syncedLyrics"]
        return results[0].get("plainLyrics", "")
    except Exception:
        return ""
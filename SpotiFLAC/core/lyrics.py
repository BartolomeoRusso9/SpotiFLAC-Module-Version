# SpotiFLAC/core/lyrics.py
"""
Multi-provider lyrics fetcher.

Ordine di tentativo (configurabile):
  1. Spotify Web  — testo sincronizzato LRC (richiede sp_dc cookie)
  2. Musixmatch   — testo sincronizzato / plain (token configurabile)
  3. Apple Music  — testo plain via iTunes affiliate endpoint
  4. Amazon Music — testo plain via afkar API
  5. LRCLIB       — testo sincronizzato / plain (nessuna auth)

Uso:
    from .lyrics import fetch_lyrics

    text = fetch_lyrics(
        "Bohemian Rhapsody", "Queen",
        album="A Night at the Opera",
        duration_s=354,
        track_id="spotify_track_id",
        providers=["spotify", "musixmatch", "lrclib"],
        spotify_token="...",   # opzionale
        musixmatch_token="...",# opzionale
    )
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Costanti                                                                     #
# --------------------------------------------------------------------------- #

_LRCLIB          = "https://lrclib.net/api"
_MUSIXMATCH_BASE = "https://apic-desktop.musixmatch.com/ws/1.1"
_SPOTIFY_LYRICS  = "https://spclient.wg.spotify.com/color-lyrics/v2/track"
_AMAZON_API_BASE = "https://amzn.afkarxyz.qzz.io/api"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_DEFAULT_PROVIDERS = ["spotify", "musixmatch", "apple", "amazon", "lrclib"]


# --------------------------------------------------------------------------- #
# Provider 1 — Spotify Web (spclient lyrics endpoint)                         #
# --------------------------------------------------------------------------- #

def _fetch_spotify(
        track_id: str,
        sp_dc_token: str,
        timeout: int = 10,
) -> str:
    """
    Recupera testo sincronizzato da Spotify via spclient.
    Richiede un token 'sp_dc' (cookie di sessione Spotify, non l'access token).

    Come ottenere sp_dc:
      1. Accedi a https://open.spotify.com nel browser
      2. Apri DevTools → Application → Cookies → sp_dc
      3. Copia il valore e passalo come `spotify_token` a fetch_lyrics()
    """
    if not track_id or not sp_dc_token:
        return ""
    try:
        # Step 1: ottieni un access token client dal cookie sp_dc
        client_token = _spotify_client_token(sp_dc_token, timeout)
        if not client_token:
            return ""

        # Step 2: fetch testi
        r = requests.get(
            f"{_SPOTIFY_LYRICS}/{track_id}",
            params={"format": "json", "market": "from_token"},
            headers={
                "Authorization": f"Bearer {client_token}",
                "App-Platform":  "WebPlayer",
                "User-Agent":    _UA,
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.debug("[lyrics/spotify] HTTP %s for track %s", r.status_code, track_id)
            return ""

        data   = r.json()
        lines  = data.get("lyrics", {}).get("lines", [])
        if not lines:
            return ""

        sync_type = data.get("lyrics", {}).get("syncType", "")
        if sync_type == "LINE_SYNCED":
            # Converti in formato LRC
            lrc_lines = []
            for line in lines:
                ms    = int(line.get("startTimeMs", 0))
                m, s  = divmod(ms // 1000, 60)
                cs    = (ms % 1000) // 10
                words = line.get("words", "")
                lrc_lines.append(f"[{m:02d}:{s:02d}.{cs:02d}]{words}")
            return "\n".join(lrc_lines)

        # Testo non sincronizzato
        return "\n".join(line.get("words", "") for line in lines)

    except Exception as exc:
        logger.debug("[lyrics/spotify] %s", exc)
        return ""


def _spotify_client_token(sp_dc: str, timeout: int) -> str:
    """
    Scambia il cookie sp_dc con un Bearer token per le API client Spotify.
    """
    try:
        r = requests.get(
            "https://open.spotify.com/get_access_token",
            params={"reason": "transport", "productType": "web_player"},
            headers={
                "Cookie":     f"sp_dc={sp_dc}",
                "User-Agent": _UA,
            },
            timeout=timeout,
        )
        if r.ok:
            return r.json().get("accessToken", "")
    except Exception as exc:
        logger.debug("[lyrics/spotify] token exchange failed: %s", exc)
    return ""


# --------------------------------------------------------------------------- #
# Provider 2 — Musixmatch                                                      #
# --------------------------------------------------------------------------- #

_MUSIXMATCH_DEFAULT_TOKEN = ""  # Imposta qui un token o passalo a runtime

def _fetch_musixmatch(
        track_name:  str,
        artist_name: str,
        album_name:  str = "",
        duration_s:  int = 0,
        token:       str = "",
        timeout:     int = 12,
) -> str:
    """
    Recupera testi sincronizzati da Musixmatch.

    Come ottenere il token:
      1. Installa l'app desktop Musixmatch (Windows/Mac)
      2. Apri DevTools → Network → cerca 'usertoken' nelle request headers
      3. Oppure usa https://github.com/akashrchandran/spotify-lyrics-api
         che espone un endpoint compatibile senza token personale.
    """
    used_token = token or _MUSIXMATCH_DEFAULT_TOKEN
    if not used_token:
        return ""

    try:
        # Tentativo con subtitle (synced lyrics)
        result = _musixmatch_subtitles(track_name, artist_name, album_name, duration_s, used_token, timeout)
        if result:
            return result
        # Fallback su lyrics plain
        return _musixmatch_plain(track_name, artist_name, used_token, timeout)
    except Exception as exc:
        logger.debug("[lyrics/musixmatch] %s", exc)
        return ""


def _musixmatch_base_params(token: str) -> dict:
    return {
        "format":   "json",
        "app_id":   "web-desktop-app-v1.0",
        "usertoken": token,
    }


def _musixmatch_subtitles(
        title:    str,
        artist:   str,
        album:    str,
        duration: int,
        token:    str,
        timeout:  int,
) -> str:
    params = {
        **_musixmatch_base_params(token),
        "namespace":      "lyrics_richsynced",
        "optional_calls": "track.richsync",
        "q_track":        title,
        "q_artist":       artist,
    }
    if album:    params["q_album"]    = album
    if duration: params["q_duration"] = duration

    r = requests.get(
        f"{_MUSIXMATCH_BASE}/macro.subtitles.get",
        params=params,
        headers={"User-Agent": _UA},
        timeout=timeout,
    )
    if not r.ok:
        return ""

    body    = r.json().get("message", {}).get("body", {})
    macro   = body.get("macro_calls", {})

    # Prova prima richsync (word-level)
    richsync = (
        macro.get("track.richsync.get", {})
        .get("message", {})
        .get("body", {})
        .get("richsync", {})
    )
    if richsync.get("richsync_body"):
        return _richsync_to_lrc(richsync["richsync_body"])

    # Poi subtitle (line-level)
    subtitle = (
        macro.get("track.subtitles.get", {})
        .get("message", {})
        .get("body", {})
        .get("subtitle_list", [{}])[0]
        .get("subtitle", {})
    )
    return subtitle.get("subtitle_body", "")


def _musixmatch_plain(title: str, artist: str, token: str, timeout: int) -> str:
    params = {
        **_musixmatch_base_params(token),
        "q_track":  title,
        "q_artist": artist,
    }
    r = requests.get(
        f"{_MUSIXMATCH_BASE}/matcher.lyrics.get",
        params=params,
        headers={"User-Agent": _UA},
        timeout=timeout,
    )
    if not r.ok:
        return ""
    return (
        r.json()
        .get("message", {})
        .get("body", {})
        .get("lyrics", {})
        .get("lyrics_body", "")
        .replace("******* This Lyrics is NOT for Commercial use *******", "")
        .strip()
    )


def _richsync_to_lrc(richsync_body: str) -> str:
    """Converte richsync JSON (word-level) in LRC (line-level)."""
    import json as _json
    try:
        lines = _json.loads(richsync_body)
        lrc: list[str] = []
        for entry in lines:
            ts  = float(entry.get("ts", 0))
            m   = int(ts // 60)
            s   = int(ts % 60)
            cs  = int((ts % 1) * 100)
            # Unisci tutte le parole della riga
            text = "".join(w.get("c", "") for w in entry.get("l", []))
            if text.strip():
                lrc.append(f"[{m:02d}:{s:02d}.{cs:02d}]{text}")
        return "\n".join(lrc)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Provider 3 — Apple Music (iTunes affiliate endpoint)                         #
# --------------------------------------------------------------------------- #

def _fetch_apple_music(
        track_name:  str,
        artist_name: str,
        timeout:     int = 10,
) -> str:
    """
    Apple Music non espone testi via API pubblica.
    Questo provider usa l'iTunes Search API per verificare la presenza del brano
    ma non può restituire il testo effettivo senza chiave MusicKit developer.
    Ritorna "" — placeholder per futura integrazione con token MusicKit.
    """
    # TODO: Implementa con Apple MusicKit API se hai un developer token Apple.
    # Endpoint: https://api.music.apple.com/v1/catalog/{storefront}/songs
    # Header: Authorization: Bearer {developer_token}
    #         Music-User-Token: {music_user_token}
    logger.debug("[lyrics/apple] Apple Music lyrics richiedono MusicKit token — skipping")
    return ""


# --------------------------------------------------------------------------- #
# Provider 4 — Amazon Music (via afkar API)                                   #
# --------------------------------------------------------------------------- #

def _fetch_amazon(
        track_name:  str,
        artist_name: str,
        isrc:        str = "",
        timeout:     int = 15,
) -> str:
    """
    Prova a recuperare testi da Amazon Music tramite l'API afkar.
    Funziona solo se il brano è disponibile su Amazon Music.
    """
    if not isrc:
        return ""
    try:
        # Cerca ASIN tramite ISRC
        r = requests.get(
            f"{_AMAZON_API_BASE}/lyrics/{isrc}",
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if not r.ok:
            return ""
        data = r.json()
        lines = data.get("lines") or data.get("lyrics", [])
        if not lines:
            return ""
        # Formato LRC se sono presenti timestamp
        if isinstance(lines[0], dict):
            lrc: list[str] = []
            for line in lines:
                ts   = int(line.get("startTime", 0))
                m    = ts // 60000
                s    = (ts % 60000) // 1000
                cs   = (ts % 1000) // 10
                text = line.get("text", "")
                lrc.append(f"[{m:02d}:{s:02d}.{cs:02d}]{text}")
            return "\n".join(lrc)
        return "\n".join(str(l) for l in lines)
    except Exception as exc:
        logger.debug("[lyrics/amazon] %s", exc)
        return ""


# --------------------------------------------------------------------------- #
# Provider 5 — LRCLIB (originale, invariato)                                  #
# --------------------------------------------------------------------------- #

def _fetch_lrclib(
        track_name:  str,
        artist_name: str,
        album_name:  str = "",
        duration_s:  int = 0,
        timeout:     int = 10,
) -> str:
    result = _lrclib_exact(track_name, artist_name, album_name, duration_s, timeout)
    if result:
        return result
    if album_name:
        result = _lrclib_exact(track_name, artist_name, "", duration_s, timeout)
        if result:
            return result
    return _lrclib_search(track_name, artist_name, timeout)


def _lrclib_exact(track: str, artist: str, album: str, duration: int, timeout: int) -> str:
    params: dict[str, Any] = {"artist_name": artist, "track_name": track}
    if album:    params["album_name"] = album
    if duration: params["duration"]   = duration
    try:
        r = requests.get(f"{_LRCLIB}/get", params=params, timeout=timeout)
        if r.status_code != 200:
            return ""
        d = r.json()
        return d.get("syncedLyrics") or d.get("plainLyrics") or ""
    except Exception:
        return ""


def _lrclib_search(track: str, artist: str, timeout: int) -> str:
    try:
        r = requests.get(
            f"{_LRCLIB}/search",
            params={"artist_name": artist, "track_name": track},
            timeout=timeout,
        )
        if r.status_code != 200:
            return ""
        results = r.json()
        if not results:
            return ""
        for item in results:
            if item.get("syncedLyrics"):
                return item["syncedLyrics"]
        return results[0].get("plainLyrics", "")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def fetch_lyrics(
        track_name:       str,
        artist_name:      str,
        album_name:       str  = "",
        duration_s:       int  = 0,
        track_id:         str  = "",
        isrc:             str  = "",
        providers:        list[str] | None = None,
        spotify_token:    str  = "",
        musixmatch_token: str  = "",
) -> str:
    """
    Cerca testi da più provider nell'ordine specificato.
    Ritorna il primo risultato non vuoto.

    Args:
        track_name:        Titolo del brano.
        artist_name:       Artista principale.
        album_name:        Album (migliora accuratezza LRCLIB/Musixmatch).
        duration_s:        Durata in secondi (migliora accuratezza).
        track_id:          Spotify track ID (per Spotify lyrics).
        isrc:              ISRC (per Amazon/Musixmatch).
        providers:         Ordine dei provider. Default: tutti.
        spotify_token:     Cookie sp_dc di Spotify (opzionale).
        musixmatch_token:  Token Musixmatch (opzionale).

    Returns:
        Testo in formato LRC o plain, oppure "" se non trovato.
    """
    if providers is None:
        providers = _DEFAULT_PROVIDERS

    for provider in providers:
        result = ""
        try:
            if provider == "spotify":
                result = _fetch_spotify(track_id, spotify_token)

            elif provider == "musixmatch":
                result = _fetch_musixmatch(
                    track_name, artist_name, album_name, duration_s,
                    token=musixmatch_token,
                )

            elif provider == "apple":
                result = _fetch_apple_music(track_name, artist_name)

            elif provider == "amazon":
                result = _fetch_amazon(track_name, artist_name, isrc=isrc)

            elif provider == "lrclib":
                result = _fetch_lrclib(track_name, artist_name, album_name, duration_s)

            else:
                logger.warning("[lyrics] unknown provider: %s", provider)

        except Exception as exc:
            logger.debug("[lyrics/%s] unexpected error: %s", provider, exc)

        if result and result.strip():
            logger.debug("[lyrics] found via %s (%d chars)", provider, len(result))
            return result.strip()

    logger.debug("[lyrics] not found for '%s' by '%s'", track_name, artist_name)
    return ""


def set_musixmatch_token(token: str) -> None:
    """Imposta il token Musixmatch globalmente (alternativa al passarlo ogni volta)."""
    global _MUSIXMATCH_DEFAULT_TOKEN
    _MUSIXMATCH_DEFAULT_TOKEN = token
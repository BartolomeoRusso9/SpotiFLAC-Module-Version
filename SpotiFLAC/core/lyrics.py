"""
Multi-provider lyrics fetcher.

Ordine di tentativo (configurabile via DEFAULT_LYRICS_PROVIDERS):
  1. Spotify Web  — testo sincronizzato LRC (autenticazione anonima via TOTP, nessun token richiesto)
  2. Apple Music  — testo sincronizzato LRC via paxsenix proxy
  3. Musixmatch   — testo sincronizzato / plain via paxsenix proxy
  4. Amazon Music — testo plain via API
  5. LRCLIB       — testo sincronizzato / plain

MODIFICA: il provider Spotify ora usa autenticazione anonima tramite TOTP
(stessa logica di index.js). Non è più richiesto alcun cookie sp_dc.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import urllib.parse

import requests

from ..providers.amazon import API_ENDPOINTS

DEFAULT_LYRICS_PROVIDERS  = ["spotify", "apple", "musixmatch", "lrclib", "amazon"]
DEFAULT_ENRICH_PROVIDERS  = ["deezer", "apple", "qobuz", "tidal", "soundcloud"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def simplify_track_name(name: str) -> str:
    patterns = [
        r'\s*\(feat\..*?\)', r'\s*\(ft\..*?\)', r'\s*\(featuring.*?\)', r'\s*\(with.*?\)',
        r'\s*-\s*Remaster(ed)?.*$', r'\s*-\s*\d{4}\s*Remaster.*$',
        r'\s*\(Remaster(ed)?.*?\)', r'\s*\(Deluxe.*?\)', r'\s*\(Bonus.*?\)',
        r'\s*\(Live.*?\)', r'\s*\(Acoustic.*?\)', r'\s*\(Radio Edit\)', r'\s*\(Single Version\)'
    ]
    result = name
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    return result.strip() or name


def get_primary_artist(name: str) -> str:
    separators = [", ", "; ", " & ", " feat. ", " ft. ", " featuring ", " with "]
    result = name
    for sep in separators:
        idx = result.lower().find(sep)
        if idx > 0:
            result = result[:idx]
            break
    return result.strip()


def normalize_loose_string(text: str) -> str:
    text = text.lower().strip()
    text = text.replace('ß', 'ss').replace('đ', 'dj').replace('æ', 'ae').replace('œ', 'oe')
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    text = re.sub(r'[/\\_\-|.&+]', ' ', text)
    return ' '.join(text.split())


def add_lrc_metadata(lrc_text: str, track_name: str, artist_name: str) -> str:
    if not lrc_text or "[ti:" in lrc_text:
        return lrc_text
    headers = f"[ti:{track_name}]\n[ar:{artist_name}]\n[by:SpotiFLAC]\n\n"
    return headers + lrc_text


logger = logging.getLogger(__name__)

_LRCLIB          = "https://lrclib.net/api"
_SPOTIFY_LYRICS  = "https://spclient.wg.spotify.com/color-lyrics/v2/track"
_PAXSENIX_APPLE  = "https://lyrics.paxsenix.org/apple-music"
_PAXSENIX_MXM    = "https://lyrics.paxsenix.org/musixmatch"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_ID_BASED_PROVIDERS = {"spotify", "amazon"}

# Cache della sessione Spotify anonima (accessToken + clientToken)
_spotify_session_cache: dict = {}


# --------------------------------------------------------------------------- #
# Provider 1 — Spotify Web (autenticazione anonima TOTP)        #
# --------------------------------------------------------------------------- #

def _get_spotify_anon_token(timeout: int = 7) -> str:
    """
    Ottiene un access token Spotify in modo anonimo usando TOTP.
    Usa una sessione con cookie persistenti per evitare chiamate ripetute.
    """
    global _spotify_session_cache

    # Reuse cached token if still valid (Spotify token dura ~1h)
    import time as _time
    cached = _spotify_session_cache.get("token")
    cached_at = _spotify_session_cache.get("cached_at", 0)
    if cached and (_time.time() - cached_at) < 3000:
        return cached

    try:
        session = _spotify_session_cache.get("session")
        if session is None:
            session = requests.Session()
            _spotify_session_cache["session"] = session

        # Step 1: visita open.spotify.com per ottenere i cookie di sessione (sp_t, ecc.)
        session.get(
            "https://open.spotify.com",
            headers={"User-Agent": _UA},
            timeout=timeout,
        )

        # Step 2: genera TOTP
        totp_headers: dict[str, str] = {}
        try:
            from .spotify_totp import generate_spotify_totp
            code, version = generate_spotify_totp()
            if code:
                totp_headers["Spotify-TOTP"]    = code
                totp_headers["Spotify-TOTP-V2"] = f"{code}:{version}"
        except Exception:
            pass

        # Step 3: ottieni access token anonimo
        r = session.get(
            "https://open.spotify.com/api/token",
            params={"reason": "init", "productType": "web-player"},
            headers={"User-Agent": _UA, **totp_headers},
            timeout=timeout,
        )
        if r.ok:
            token = r.json().get("accessToken", "")
            if token:
                _spotify_session_cache["token"]     = token
                _spotify_session_cache["cached_at"] = _time.time()
                return token
    except Exception as exc:
        logger.debug("[lyrics/spotify] anon token failed: %s", exc)

    return ""


def _fetch_spotify(track_id: str, timeout: int = 7) -> str:
    """Scarica i testi da Spotify usando autenticazione anonima TOTP."""
    if not track_id:
        return ""
    try:
        access_token = _get_spotify_anon_token(timeout)
        if not access_token:
            return ""

        r = requests.get(
            f"{_SPOTIFY_LYRICS}/{track_id}",
            params={"format": "json", "market": "from_token"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "App-Platform":  "WebPlayer",
                "User-Agent":    _UA,
            },
            timeout=timeout,
        )
        if r.status_code == 401:
            # Invalida solo il token, non la sessione con i cookie
            _spotify_session_cache.pop("token", None)
            _spotify_session_cache.pop("cached_at", None)
            access_token = _get_spotify_anon_token(timeout)
            if not access_token:
                return ""
            r = requests.get(
                f"{_SPOTIFY_LYRICS}/{track_id}",
                params={"format": "json", "market": "from_token"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "App-Platform":  "WebPlayer",
                    "User-Agent":    _UA,
                },
                timeout=timeout,
            )
        if r.status_code != 200:
            return ""

        data  = r.json()
        lines = data.get("lyrics", {}).get("lines", [])
        if not lines:
            return ""

        sync_type = data.get("lyrics", {}).get("syncType", "")
        if sync_type == "LINE_SYNCED":
            lrc_lines = []
            for line in lines:
                ms   = int(line.get("startTimeMs", 0))
                m, s = divmod(ms // 1000, 60)
                cs   = (ms % 1000) // 10
                words = line.get("words", "")
                lrc_lines.append(f"[{m:02d}:{s:02d}.{cs:02d}]{words}")
            return "\n".join(lrc_lines)

        return "\n".join(line.get("words", "") for line in lines)

    except Exception as exc:
        logger.debug("[lyrics/spotify] %s", exc)
        return ""


# --------------------------------------------------------------------------- #
# Provider 2 — Apple Music (Paxsenix Proxy)                                    #
# --------------------------------------------------------------------------- #

def _score_apple_result(res: dict, t_name: str, a_name: str, duration_s: int) -> int:
    score = 0
    r_t = normalize_loose_string(res.get("songName", ""))
    r_a = normalize_loose_string(res.get("artistName", ""))
    t_t = normalize_loose_string(t_name)
    t_a = normalize_loose_string(a_name)
    if r_t == t_t: score += 50
    elif t_t in r_t or r_t in t_t: score += 25
    if r_a == t_a: score += 60
    elif t_a in r_a or r_a in t_a: score += 30
    r_dur = res.get("duration", 0)
    if duration_s > 0 and r_dur > 0:
        diff = abs((r_dur / 1000.0) - duration_s)
        if diff <= 5:
            score += 20
    return score


def _fetch_apple(track_name: str, artist_name: str, duration_s: int, timeout: int = 7) -> str:
    query = urllib.parse.quote(f"{track_name} {artist_name}")
    search_url = f"{_PAXSENIX_APPLE}/search?q={query}"
    try:
        r = requests.get(search_url, headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=timeout)
        if not r.ok: return ""
        results = r.json()
        if not results: return ""
        best = max(results, key=lambda x: _score_apple_result(x, track_name, artist_name, duration_s))
        song_id = best.get("id")
        if not song_id: return ""
        lyrics_url = f"{_PAXSENIX_APPLE}/lyrics?id={song_id}"
        r_lyr = requests.get(lyrics_url, headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=timeout)
        if not r_lyr.ok: return ""
        data = r_lyr.json()
        content = data.get("content", []) if isinstance(data, dict) else data
        lrc_lines = []
        for line in content:
            ts = int(line.get("timestamp", 0))
            m, s = divmod(ts // 1000, 60)
            cs = (ts % 1000) // 10
            text_parts = line.get("text", [])
            line_text = ""
            for part in text_parts:
                line_text += part.get("text", "")
                if not part.get("part", False):
                    line_text += " "
            line_text = line_text.strip()
            if line_text:
                lrc_lines.append(f"[{m:02d}:{s:02d}.{cs:02d}]{line_text}")
        return "\n".join(lrc_lines)
    except Exception as exc:
        logger.debug("[lyrics/apple] %s", exc)
        return ""


# --------------------------------------------------------------------------- #
# Provider 3 — Musixmatch (Paxsenix Proxy)                                     #
# --------------------------------------------------------------------------- #

def _fetch_musixmatch(track_name: str, artist_name: str, duration_s: int, timeout: int = 7) -> str:
    for sync_type in ["word", "line"]:
        params = {"t": track_name, "a": artist_name, "type": sync_type, "format": "lrc"}
        if duration_s > 0:
            params["d"] = str(duration_s)
        url = f"{_PAXSENIX_MXM}/lyrics?" + urllib.parse.urlencode(params)
        try:
            r = requests.get(url, headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=timeout)
            if r.ok:
                body = r.text.strip()
                import json
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, str) and parsed.strip():
                        return parsed.strip()
                    if isinstance(parsed, dict):
                        for key in ("lrc", "lyrics", "syncedLyrics", "plainLyrics"):
                            val = parsed.get(key)
                            if isinstance(val, str) and val.strip():
                                return val.strip()
                except ValueError:
                    if body and not body.startswith("{"):
                        return body
        except Exception as exc:
            logger.debug("[lyrics/musixmatch] %s fallito: %s", sync_type, exc)
    return ""


# --------------------------------------------------------------------------- #
# Provider 4 — Amazon Music                                                    #
# --------------------------------------------------------------------------- #

def _fetch_amazon(isrc: str, timeout: int = 7) -> str:
    if not isrc:
        return ""
    try:
        r = requests.get(
            f"{API_ENDPOINTS['spotbye']}/lyrics/{isrc}",
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if not r.ok: return ""
        data  = r.json()
        lines = data.get("lines") or data.get("lyrics", [])
        if not lines: return ""
        if isinstance(lines[0], dict):
            lrc = []
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
# Provider 5 — LRCLIB                                                          #
# --------------------------------------------------------------------------- #

def _fetch_lrclib(track_name: str, artist_name: str, album_name: str = "", duration_s: int = 0, timeout: int = 7) -> str:
    def _lrclib_exact(t, a, al, d):
        params = {"artist_name": a, "track_name": t}
        if al: params["album_name"] = al
        if d:  params["duration"]   = d
        try:
            r = requests.get(f"{_LRCLIB}/get", params=params, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                return data.get("syncedLyrics") or data.get("plainLyrics") or ""
        except Exception: pass
        return ""

    result = _lrclib_exact(track_name, artist_name, album_name, duration_s)
    if result: return result
    if album_name:
        result = _lrclib_exact(track_name, artist_name, "", duration_s)
        if result: return result

    try:
        r = requests.get(
            f"{_LRCLIB}/search",
            params={"artist_name": artist_name, "track_name": track_name},
            timeout=timeout,
        )
        if r.status_code == 200:
            results = r.json()
            if results:
                best_synced = best_plain = None
                for item in results:
                    item_duration = item.get("duration", 0)
                    if duration_s == 0 or abs(item_duration - duration_s) <= 10.0:
                        if item.get("syncedLyrics") and not best_synced:
                            best_synced = item["syncedLyrics"]
                        elif item.get("plainLyrics") and not best_plain:
                            best_plain = item["plainLyrics"]
                return best_synced or best_plain or ""
    except Exception: pass
    return ""


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def fetch_lyrics(
        track_name:    str,
        artist_name:   str,
        album_name:    str  = "",
        duration_s:    int  = 0,
        track_id:      str  = "",
        isrc:          str  = "",
        providers:     list[str] | None = None,
) -> tuple[str, str]:
    if providers is None:
        providers = DEFAULT_LYRICS_PROVIDERS

    clean_track  = simplify_track_name(track_name)
    clean_artist = get_primary_artist(artist_name)

    _short = clean_track.split(" - ", 1)[-1].strip() if " - " in clean_track else None
    short_track = _short if _short and _short != clean_track else None

    for provider in providers:
        titles_to_try = [clean_track]
        if short_track and provider not in _ID_BASED_PROVIDERS:
            titles_to_try.append(short_track)

        for title in titles_to_try:
            result = ""
            try:
                if provider == "spotify":
                    spotify_id = track_id if (track_id and len(track_id) == 22 and "_" not in track_id) else ""
                    result = _fetch_spotify(spotify_id)
                elif provider == "apple":
                    result = _fetch_apple(title, clean_artist, duration_s)
                elif provider == "musixmatch":
                    result = _fetch_musixmatch(title, clean_artist, duration_s)
                elif provider == "amazon":
                    result = _fetch_amazon(isrc)
                elif provider == "lrclib":
                    result = _fetch_lrclib(title, clean_artist, album_name, duration_s)
                else:
                    logger.warning("[lyrics] unknown provider: %s", provider)
                    break
            except Exception as exc:
                logger.debug("[lyrics/%s] unexpected error: %s", provider, exc)

            if result and result.strip():
                label = f"{provider}" + (f" [short title]" if title == short_track else "")
                logger.debug("[lyrics] found via %s (%d chars)", label, len(result))
                return add_lrc_metadata(result.strip(), track_name, artist_name), provider

            if provider in _ID_BASED_PROVIDERS:
                break

    logger.debug("[lyrics] not found for '%s' by '%s'", track_name, artist_name)
    return "", ""
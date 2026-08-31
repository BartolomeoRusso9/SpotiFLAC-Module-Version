"""Async multi-provider lyrics fetcher."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass

from . import get_amazon_endpoint
from .http import NetworkManager
from .response_cache import get as get_cached_response
from .response_cache import put as put_cached_response


@dataclass(slots=True)
class LyricsContext:
    track_name: str
    artist_name: str
    album_name: str
    duration_s: int
    spotify_id: str
    isrc: str

    @property
    def clean_track(self) -> str:
        return simplify_track_name(self.track_name)

    @property
    def clean_artist(self) -> str:
        return get_primary_artist(self.artist_name)


DEFAULT_LYRICS_PROVIDERS = ["apple", "lrclib"]
DEFAULT_ENRICH_PROVIDERS = ["deezer", "apple", "qobuz", "tidal"]


# ---------------------------------------------------------------------------
# Helpers (unchanged)
# ---------------------------------------------------------------------------


def simplify_track_name(name: str) -> str:
    patterns = [
        r"\s*\(feat\..*?\)",
        r"\s*\(ft\..*?\)",
        r"\s*\(featuring.*?\)",
        r"\s*\(with.*?\)",
        r"\s*-\s*Remaster(ed)?.*$",
        r"\s*-\s*\d{4}\s*Remaster.*$",
        r"\s*\(Remaster(ed)?.*?\)",
        r"\s*\(Deluxe.*?\)",
        r"\s*\(Bonus.*?\)",
        r"\s*\(Live.*?\)",
        r"\s*\(Acoustic.*?\)",
        r"\s*\(Radio Edit\)",
        r"\s*\(Single Version\)",
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
    text = (
        text.replace("ß", "ss").replace("đ", "dj").replace("æ", "ae").replace("œ", "oe")
    )
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    text = re.sub(r"[/\\_\-|.&+]", " ", text)
    return " ".join(text.split())


def add_lrc_metadata(lrc_text: str, track_name: str, artist_name: str) -> str:
    if not lrc_text or "[ti:" in lrc_text:
        return lrc_text
    headers = f"[ti:{track_name}]\n[ar:{artist_name}]\n[by:SpotiFLAC]\n\n"
    return headers + lrc_text


def _format_lrc_timestamp(milliseconds: int, opening: str = "[") -> str:
    minutes, remainder = divmod(max(0, milliseconds), 60_000)
    seconds, remainder = divmod(remainder, 1_000)
    centiseconds = remainder // 10
    closing = ">" if opening == "<" else "]"
    return f"{opening}{minutes:02d}:{seconds:02d}.{centiseconds:02d}{closing}"


logger = logging.getLogger(__name__)

_LRCLIB = "https://lrclib.net/api"
_SPOTIFY_LYRICS = "https://spclient.wg.spotify.com/color-lyrics/v2/track"
_ITUNES_SEARCH = "https://itunes.apple.com/search"
_PAXSENIX_APPLE = "https://lyrics.paxsenix.org/apple-music/lyrics"
_PAXSENIX_MXM = "https://lyrics.paxsenix.org/musixmatch"
_PAXSENIX_BASE = "https://lyrics.paxsenix.org"
_DEEZER_SEARCH = "https://api.deezer.com/search/track"
_GENIUS_SEARCH = "https://genius.com/api/search/multi"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/145.0.0.0 Safari/537.36"
_LYRICS_RESPONSE_CACHE_TTL = 7 * 24 * 60 * 60

#: A provider that had nothing for a track is remembered too, but briefly.
#: With the order authoritative, a first choice that keeps coming up empty
#: would otherwise be asked again for every track of every run; a catalogue
#: that gains the lyrics later still gets noticed the same day.
_LYRICS_MISS_CACHE_TTL = 6 * 60 * 60


# ---------------------------------------------------------------------------
# Spotify anon token (sync helper, reused by async)
# ---------------------------------------------------------------------------

_spotify_session_cache: dict[str, object] = {}
_spotify_token_lock: asyncio.Lock | None = None


async def _get_spotify_lock() -> asyncio.Lock:
    global _spotify_token_lock

    if _spotify_token_lock is None:
        _spotify_token_lock = asyncio.Lock()

    return _spotify_token_lock


async def _get_spotify_anon_token(timeout: int = 7) -> str:
    import time

    cached = _spotify_session_cache.get("token")
    cached_at = _spotify_session_cache.get("cached_at", 0)

    if cached and (time.time() - cached_at) < 3000:
        return str(cached)

    lock = await _get_spotify_lock()

    async with lock:
        cached = _spotify_session_cache.get("token")
        cached_at = _spotify_session_cache.get("cached_at", 0)

        if cached and (time.time() - cached_at) < 3000:
            return str(cached)

        try:
            client = await NetworkManager.get_async_client_safe()

            totp_headers: dict[str, str] = {}

            try:
                from .spotify_totp import generate_spotify_totp

                code, version = generate_spotify_totp()

                if code:
                    totp_headers["Spotify-TOTP"] = code
                    totp_headers["Spotify-TOTP-V2"] = f"{code}:{version}"

            except Exception:
                pass

            await client.get(
                "https://open.spotify.com",
                headers={"User-Agent": _UA},
                timeout=timeout,
            )

            r = await client.get(
                "https://open.spotify.com/api/token",
                params={
                    "reason": "init",
                    "productType": "web-player",
                },
                headers={
                    "User-Agent": _UA,
                    **totp_headers,
                },
                timeout=timeout,
            )

            if not r.is_success:
                return ""

            token = r.json().get("accessToken")

            if not token:
                return ""

            _spotify_session_cache["token"] = token
            _spotify_session_cache["cached_at"] = time.time()

            return token

        except Exception as exc:
            logger.debug("[lyrics/spotify] anon token failed: %s", exc)
            return ""


# ---------------------------------------------------------------------------
# Async fetch functions (Phase 2 — new)
# ---------------------------------------------------------------------------


async def _invalidate_spotify_token() -> None:
    lock = await _get_spotify_lock()

    async with lock:
        _spotify_session_cache.pop("token", None)
        _spotify_session_cache.pop("cached_at", None)


async def _fetch_spotify_async(track_id: str, timeout: int = 7) -> str:
    if not track_id:
        return ""
    try:
        access_token = await _get_spotify_anon_token(timeout)
        if not access_token:
            return ""
        client = await NetworkManager.get_async_client_safe()
        r = await client.get(
            f"{_SPOTIFY_LYRICS}/{track_id}",
            params={"format": "json", "market": "from_token"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "App-Platform": "WebPlayer",
                "User-Agent": _UA,
            },
            timeout=timeout,
        )
        if r.status_code == 401:
            await _invalidate_spotify_token()

            access_token = await _get_spotify_anon_token(timeout)

            if not access_token:
                return ""
            r = await client.get(
                f"{_SPOTIFY_LYRICS}/{track_id}",
                params={"format": "json", "market": "from_token"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "App-Platform": "WebPlayer",
                    "User-Agent": _UA,
                },
                timeout=timeout,
            )
        if r.status_code != 200:
            return ""

        data = r.json()
        lines = data.get("lyrics", {}).get("lines", [])
        if not lines:
            return ""
        sync_type = data.get("lyrics", {}).get("syncType", "")
        if sync_type == "LINE_SYNCED":
            lrc_lines = []
            for line in lines:
                ms = int(line.get("startTimeMs", 0))
                m, s = divmod(ms // 1000, 60)
                cs = (ms % 1000) // 10
                lrc_lines.append(f"[{m:02d}:{s:02d}.{cs:02d}]{line.get('words', '')}")
            return "\n".join(lrc_lines)
        return "\n".join(line.get("words", "") for line in lines)
    except Exception as exc:
        logger.debug("[lyrics/spotify] async: %s", exc)
        return ""


def _apple_payload_to_lrc(data: object) -> str:
    """Apple's timed-lyrics payload as enhanced LRC.

    Apple times every *syllable*, not every word, which is what makes a
    word-by-word display possible — so each part becomes its own inline
    `<mm:ss.xx>` tag after the line's own `[mm:ss.xx]`. A part flagged
    `part: true` continues the syllable before it ("ex|pres|sions") and gets
    no space, or the word would be rendered broken apart.
    """
    content = data.get("content", []) if isinstance(data, dict) else data
    if not isinstance(content, list):
        return ""

    lrc_lines = []
    for line in content:
        if not isinstance(line, dict):
            continue
        ts = int(line.get("timestamp", 0))
        word_parts = []
        for part in line.get("text", []) or []:
            if not isinstance(part, dict):
                continue
            part_text = part.get("text", "")
            if not part_text:
                continue
            part_timestamp = int(part.get("timestamp", ts))
            separator = "" if part.get("part", False) else " "
            word_parts.append(
                f"{separator}{_format_lrc_timestamp(part_timestamp, '<')}{part_text}"
            )
        line_text = "".join(word_parts).strip()
        if line_text:
            lrc_lines.append(f"{_format_lrc_timestamp(ts)}{line_text}")
    return "\n".join(lrc_lines)


async def _fetch_apple_async(
    track_name: str,
    artist_name: str,
    duration_s: int,
    isrc: str = "",
    timeout: int = 7,
) -> str:
    try:
        client = await NetworkManager.get_async_client_safe()
        search_params = {
            "term": f"{track_name} {artist_name}",
            "media": "music",
            "entity": "song",
            "limit": 5,
            "country": "US",
        }
        r = await client.get(
            _ITUNES_SEARCH,
            params=search_params,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not r.is_success:
            return ""
        results = r.json().get("results", [])
        if not results:
            return ""

        scored = [
            (res, _score_itunes_result(res, track_name, artist_name, duration_s))
            for res in results
        ]
        best_result, best_score = max(scored, key=lambda x: x[1])

        if best_score < 50:
            return ""

        song_id = best_result.get("trackId")
        if not song_id:
            return ""
        r_lyr = await client.get(
            _PAXSENIX_APPLE,
            params={"id": str(song_id)},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not r_lyr.is_success:
            return ""
        return _apple_payload_to_lrc(r_lyr.json())
    except Exception as exc:
        logger.debug("[lyrics/apple] async: %s", exc)
        return ""


def _score_itunes_result(
    res: dict,
    track_name: str,
    artist_name: str,
    duration_s: int,
) -> int:
    score = 0
    result_track = normalize_loose_string(res.get("trackName", ""))
    result_artist = normalize_loose_string(res.get("artistName", ""))
    wanted_track = normalize_loose_string(track_name)
    wanted_artist = normalize_loose_string(artist_name)
    if result_track == wanted_track:
        score += 50
    elif wanted_track in result_track or result_track in wanted_track:
        score += 25
    if result_artist == wanted_artist:
        score += 60
    elif wanted_artist in result_artist or result_artist in wanted_artist:
        score += 30
    result_duration = res.get("trackTimeMillis", 0)
    if (
        duration_s > 0
        and result_duration > 0
        and abs((result_duration / 1000.0) - duration_s) <= 5
    ):
        score += 20
    return score


async def _fetch_musixmatch_async(
    track_name: str,
    artist_name: str,
    duration_s: int,
    timeout: int = 7,
) -> str:
    import json as _json

    client = await NetworkManager.get_async_client_safe()
    for sync_type in ["word"]:
        params = {"t": track_name, "a": artist_name, "type": sync_type, "format": "lrc"}
        if duration_s > 0:
            params["d"] = str(duration_s)
        url = f"{_PAXSENIX_MXM}/lyrics?" + urllib.parse.urlencode(params)
        try:
            r = await client.get(
                url,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=timeout,
            )
            if r.is_success:
                body = r.text.strip()
                try:
                    parsed = _json.loads(body)
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
            logger.debug("[lyrics/musixmatch] async %s failed: %s", sync_type, exc)
    return ""


def _first_search_item(value: object) -> dict | None:
    if isinstance(value, list):
        for item in value:
            found = _first_search_item(item)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    if any(
        key in value for key in ("id", "songId", "songmid", "videoId", "hash", "url")
    ):
        return value
    for key in ("results", "data", "items", "tracks", "songs", "videos"):
        found = _first_search_item(value.get(key))
        if found:
            return found
    return None


def _lyrics_from_payload(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        text = "\n".join(
            item if isinstance(item, str) else _lyrics_from_payload(item)
            for item in value
        )
        return text.strip()
    if not isinstance(value, dict):
        return ""
    for key in (
        "lrc",
        "lyrics",
        "syncedLyrics",
        "plainLyrics",
        "content",
        "data",
        "result",
    ):
        lyrics = _lyrics_from_payload(value.get(key))
        if lyrics:
            return lyrics
    return ""


async def _fetch_paxsenix_search_provider_async(
    provider: str,
    track_name: str,
    artist_name: str,
    duration_s: int,
    timeout: int = 7,
) -> str:
    client = await NetworkManager.get_async_client_safe()
    try:
        search = await client.get(
            f"{_PAXSENIX_BASE}/{provider}/search",
            params={"q": f"{track_name} {artist_name}"},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not search.is_success:
            return ""
        item = _first_search_item(search.json())
        if not item:
            return ""
        id_keys = {
            "netease": ("id", "songId"),
            "qq": ("songmid", "songId", "id"),
            "youtube": ("videoId", "id"),
            "kugou": ("hash", "id"),
        }
        provider_id = next(
            (item.get(key) for key in id_keys[provider] if item.get(key)),
            None,
        )
        if not provider_id:
            return ""
        params = {"id": str(provider_id), "v": "1"}
        if provider in {"netease", "kugou"}:
            params["word"] = "true"
        lyrics = await client.get(
            f"{_PAXSENIX_BASE}/{provider}/lyrics",
            params=params,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not lyrics.is_success:
            return ""
        return _lyrics_from_payload(lyrics.json())
    except Exception as exc:
        logger.debug("[lyrics/%s] async: %s", provider, exc)
        return ""


async def _fetch_deezer_async(
    track_name: str,
    artist_name: str,
    timeout: int = 7,
) -> str:
    try:
        client = await NetworkManager.get_async_client_safe()
        search = await client.get(
            _DEEZER_SEARCH,
            params={"q": f"track:{track_name} artist:{artist_name}", "limit": 5},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not search.is_success:
            return ""
        item = _first_search_item(search.json())
        if not item or not item.get("id"):
            return ""
        response = await client.get(
            f"{_PAXSENIX_BASE}/deezer/lyrics",
            params={"id": str(item["id"]), "v": "1"},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not response.is_success:
            return ""
        return _lyrics_from_payload(response.json())
    except Exception as exc:
        logger.debug("[lyrics/deezer] async: %s", exc)
        return ""


async def _fetch_genius_async(
    track_name: str,
    artist_name: str,
    timeout: int = 7,
) -> str:
    try:
        client = await NetworkManager.get_async_client_safe()
        search = await client.get(
            _GENIUS_SEARCH,
            params={"q": f"{track_name} {artist_name}", "per_page": 5},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not search.is_success:
            return ""
        data = search.json()
        url = ""
        for section in data.get("response", {}).get("sections", []):
            for hit in section.get("hits", []):
                result = hit.get("result", {})
                if result.get("url"):
                    url = result["url"]
                    break
            if url:
                break
        if not url:
            return ""
        response = await client.get(
            f"{_PAXSENIX_BASE}/genius/lyrics",
            params={"url": url, "v": "1"},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not response.is_success:
            return ""
        return _lyrics_from_payload(response.json())
    except Exception as exc:
        logger.debug("[lyrics/genius] async: %s", exc)
        return ""


async def _fetch_amazon_async(isrc: str, timeout: int = 7) -> str:
    if not isrc:
        return ""
    try:
        client = await NetworkManager.get_async_client_safe()
        r = await client.get(
            f"{get_amazon_endpoint('spotbye1')}/lyrics/{isrc}",
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if not r.is_success:
            return ""
        data = r.json()
        lines = data.get("lines") or data.get("lyrics", [])
        if not lines:
            return ""
        if isinstance(lines[0], dict):
            lrc = []
            for line in lines:
                ts = int(line.get("startTime", 0))
                m = ts // 60000
                s = (ts % 60000) // 1000
                cs = (ts % 1000) // 10
                text = line.get("text", "")
                lrc.append(f"[{m:02d}:{s:02d}.{cs:02d}]{text}")
            return "\n".join(lrc)
        return "\n".join(str(ln) for ln in lines)
    except Exception as exc:
        logger.debug("[lyrics/amazon] async: %s", exc)
        return ""


async def _fetch_lrclib_async(
    track_name: str,
    artist_name: str,
    album_name: str = "",
    duration_s: int = 0,
    timeout: int = 7,
) -> str:
    client = await NetworkManager.get_async_client_safe()

    async def _exact(t: str, a: str, al: str, d: int) -> str:
        params = {"artist_name": a, "track_name": t}
        if al:
            params["album_name"] = al
        if d:
            params["duration"] = d
        try:
            r = await client.get(f"{_LRCLIB}/get", params=params, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                return data.get("syncedLyrics") or data.get("plainLyrics") or ""
        except Exception:
            pass
        return ""

    tasks = [
        _exact(
            track_name,
            artist_name,
            album_name,
            duration_s,
        ),
    ]

    if album_name:
        tasks.append(
            _exact(
                track_name,
                artist_name,
                "",
                duration_s,
            ),
        )

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, str) and result:
            return result

    try:
        r = await client.get(
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
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Async fetch_lyrics — Phase 2 (parallel, as_completed)
# ---------------------------------------------------------------------------

_PROVIDER_MAP = {
    "spotify": lambda ctx: _fetch_spotify_async(
        ctx.spotify_id,
    ),
    "apple": lambda ctx: _fetch_apple_async(
        ctx.clean_track,
        ctx.clean_artist,
        ctx.duration_s,
        ctx.isrc,
    ),
    "musixmatch": lambda ctx: _fetch_musixmatch_async(
        ctx.clean_track,
        ctx.clean_artist,
        ctx.duration_s,
    ),
    "deezer": lambda ctx: _fetch_deezer_async(
        ctx.clean_track,
        ctx.clean_artist,
    ),
    "genius": lambda ctx: _fetch_genius_async(
        ctx.clean_track,
        ctx.clean_artist,
    ),
    "netease": lambda ctx: _fetch_paxsenix_search_provider_async(
        "netease",
        ctx.clean_track,
        ctx.clean_artist,
        ctx.duration_s,
    ),
    "qq": lambda ctx: _fetch_paxsenix_search_provider_async(
        "qq",
        ctx.clean_track,
        ctx.clean_artist,
        ctx.duration_s,
    ),
    "youtube": lambda ctx: _fetch_paxsenix_search_provider_async(
        "youtube",
        ctx.clean_track,
        ctx.clean_artist,
        ctx.duration_s,
    ),
    "kugou": lambda ctx: _fetch_paxsenix_search_provider_async(
        "kugou",
        ctx.clean_track,
        ctx.clean_artist,
        ctx.duration_s,
    ),
    "amazon": lambda ctx: _fetch_amazon_async(
        ctx.isrc,
    ),
    "lrclib": lambda ctx: _fetch_lrclib_async(
        ctx.clean_track,
        ctx.clean_artist,
        ctx.album_name,
        ctx.duration_s,
    ),
}


async def fetch_lyrics_async(
    track_name: str,
    artist_name: str,
    album_name: str = "",
    duration_s: int = 0,
    track_id: str = "",
    isrc: str = "",
    providers: list[str] | None = None,
) -> tuple[str, str]:

    if providers is None:
        providers = DEFAULT_LYRICS_PROVIDERS

    # Cached per provider, not per lookup. The cache used to hold the
    # finished *decision* — "these providers, for this track, gave you
    # lrclib's text" — under a key that included the provider list. That
    # made every change to how a provider is chosen invisible for a week:
    # when the ordering fix below landed, 65 of one playlist's 66 tracks
    # were answered out of the cache with the line-level lyrics the old
    # first-past-the-post behaviour had picked the night before, and the
    # fix looked like it had done nothing. Caching each provider's own
    # answer instead spares exactly the same network calls and leaves the
    # choosing to be done fresh every time.
    track_key = "|".join(
        [track_name, artist_name, album_name, str(duration_s), track_id, isrc],
    )

    def _cached_for(provider_name: str) -> str | None:
        key = f"{provider_name}|{track_key}"
        recent = get_cached_response("lyrics-provider", key, _LYRICS_MISS_CACHE_TTL)
        if isinstance(recent, str):
            return recent
        older = get_cached_response("lyrics-provider", key, _LYRICS_RESPONSE_CACHE_TTL)
        # Past the short TTL only a hit still counts: a miss that old is
        # worth re-asking about.
        return older if isinstance(older, str) and older else None

    ctx = LyricsContext(
        track_name=track_name,
        artist_name=artist_name,
        album_name=album_name,
        duration_s=duration_s,
        spotify_id=(
            track_id if track_id and len(track_id) == 22 and "_" not in track_id else ""
        ),
        isrc=isrc,
    )

    async def run_provider(
        provider_name: str,
    ) -> tuple[str, str]:

        fetcher = _PROVIDER_MAP.get(
            provider_name,
        )

        if not fetcher:
            return "", ""

        cached = _cached_for(provider_name)
        if cached is not None:
            return (cached, provider_name) if cached else ("", "")

        try:
            result = await asyncio.wait_for(
                fetcher(ctx),
                timeout=10,
            )
            text = result.strip() if result else ""
            put_cached_response("lyrics-provider", f"{provider_name}|{track_key}", text)
            if text:
                return result, provider_name

        except Exception as exc:
            # Not cached: a timeout or a network error says nothing about
            # whether this provider has the lyrics.
            logger.debug(
                "[lyrics/%s] %s",
                provider_name,
                exc,
            )

        return "", ""

    # Every provider is asked at once, but they are *read* in the order the
    # caller listed them. as_completed() used to be the reader, which made
    # `providers` a set rather than a ranking: whoever answered first won,
    # and lrclib answers in ~0.1s against Apple's ~1s (an iTunes search, then
    # the lyrics fetch). So "apple, lrclib" reliably produced lrclib — plain
    # line-level LRC — and the word-by-word lyrics the order was asking for
    # never got used.
    #
    # Reading in order costs nothing when the first choice answers, and
    # nothing when it fails either: the others have long since finished by
    # then and their results are already waiting.
    tasks = [asyncio.create_task(run_provider(provider)) for provider in providers]

    try:
        for task in tasks:
            lyrics, provider = await task

            if not lyrics:
                continue

            result = (
                add_lrc_metadata(
                    lyrics.strip(),
                    track_name,
                    artist_name,
                ),
                provider,
            )
            logger.debug(
                "[lyrics] Lyrics found with provider '%s' for %s - %s",
                provider,
                artist_name,
                track_name,
            )
            return result

        return "", ""
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

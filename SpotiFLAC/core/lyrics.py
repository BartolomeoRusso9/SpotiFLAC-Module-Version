"""Async multi-provider lyrics fetcher."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import get_amazon_endpoint
from .cross_loop_lock import CrossLoopLock
from .http import NetworkManager
from .response_cache import get as get_cached_response
from .response_cache import put as put_cached_response

if TYPE_CHECKING:
    from .apple_music_metadata import AppleMusicMetadataClient


@dataclass(slots=True)
class LyricsContext:
    track_name: str
    artist_name: str
    album_name: str
    duration_s: int
    spotify_id: str
    isrc: str
    #: Apple times every syllable; when True its lyrics are emitted as
    #: word-by-word enhanced LRC, when False as plain line-synced LRC.
    apple_word_by_word: bool = True

    @property
    def clean_track(self) -> str:
        return simplify_track_name(self.track_name)

    @property
    def clean_artist(self) -> str:
        return get_primary_artist(self.artist_name)


DEFAULT_LYRICS_PROVIDERS = ["apple", "lrclib", "binilyrics", "unison"]
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


#: Jellyfin parses word-by-word lyrics into one cue per word and runs .NET's
#: string.Trim() over each one, which eats the space that separated the word
#: from the next; re-serialised, "Nuij simm doije" comes back out of its
#: metadata cache as "Nuijsimmdoije". U+2800 BRAILLE PATTERN BLANK draws as a
#: blank but sits in category So rather than Zs, so char.IsWhiteSpace is false
#: for it and the trim leaves it where it is.
JELLYFIN_WORD_GAP = "\u2800"

#: The `<mm:ss.xx>` that opens each word of an enhanced-LRC line. Kept as a
#: capturing group so re.split() hands back the tags along with the words.
_INLINE_WORD_TAG = re.compile(r"(<\d{2}:\d{2}\.\d{2}>)")


def apply_jellyfin_word_gap(lrc_text: str, gap: str = JELLYFIN_WORD_GAP) -> str:
    """Swap the space that ends each timed word for a trim-proof blank.

    Only the *separating* space goes: a space inside a word ("t'he 'a") is
    left alone, since a trim never reaches it, and so is the last word of a
    line, which has no separator to protect. Line-synced LRC carries no
    inline tags and comes back untouched.

    Re-running this is a no-op, but the test for that is per word rather than
    per text: a word already carrying the gap has nothing to rstrip, so it is
    left as it is. Bailing out on the first gap anywhere would instead strand
    every ungapped word in a text that is only partly converted — which is
    what lyrics stitched together from two sources look like.
    """
    if not lrc_text:
        return lrc_text

    lines = []
    for line in lrc_text.split("\n"):
        if "<" not in line:
            lines.append(line)
            continue
        # One capturing group means re.split() alternates text, tag, text,
        # tag, ... starting with text, so the odd indices are the tags.
        pieces = _INLINE_WORD_TAG.split(line)
        for index in range(0, len(pieces), 2):
            word = pieces[index]
            trimmed = word.rstrip()
            if trimmed and trimmed != word and not trimmed.endswith(gap):
                pieces[index] = trimmed + gap
        lines.append("".join(pieces))
    return "\n".join(lines)


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
#: Every track's lyrics lookup goes through here, from whichever event loop
#: that download runs on — an asyncio.Lock would bind to the first one.
_spotify_token_lock = CrossLoopLock()


async def _get_spotify_lock() -> CrossLoopLock:
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

            # Spotify wants the TOTP as query parameters, the way
            # spotfetch.py sends it. Sent as headers the endpoint answers
            # 400 "Unauthorized request" and there is no token at all.
            params = {
                "reason": "init",
                "productType": "web-player",
            }

            try:
                from .spotify_totp import generate_spotify_totp

                code, version = generate_spotify_totp()

                if code:
                    params["totp"] = code
                    params["totpVer"] = str(version)
                    params["totpServer"] = code

            except Exception:
                pass

            await client.get(
                "https://open.spotify.com",
                headers={"User-Agent": _UA},
                timeout=timeout,
            )

            r = await client.get(
                "https://open.spotify.com/api/token",
                params=params,
                headers={"User-Agent": _UA},
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


def _apple_payload_to_lrc(data: object, word_by_word: bool = True) -> str:
    """Apple's timed-lyrics payload as LRC.

    Apple times every *syllable*, not every word, which is what makes a
    word-by-word display possible — so with ``word_by_word`` each part
    becomes its own inline ``<mm:ss.xx>`` tag after the line's own
    ``[mm:ss.xx]``. A part flagged ``part: true`` continues the syllable
    before it ("ex|pres|sions") and gets no space, or the word would be
    rendered broken apart.

    With ``word_by_word`` False the per-syllable timings are dropped and the
    line is emitted as plain line-synced LRC — one ``[mm:ss.xx]`` per line
    followed by the whole line's text — for players/overlays that only
    understand line-level sync or for users who prefer it.
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
            separator = "" if part.get("part", False) else " "
            if word_by_word:
                part_timestamp = int(part.get("timestamp", ts))
                word_parts.append(
                    f"{separator}{_format_lrc_timestamp(part_timestamp, '<')}{part_text}"
                )
            else:
                word_parts.append(f"{separator}{part_text}")
        line_text = "".join(word_parts).strip()
        if line_text:
            lrc_lines.append(f"{_format_lrc_timestamp(ts)}{line_text}")
    return "\n".join(lrc_lines)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


#: One client for every direct-TTML lookup in the process. It holds the
#: developer token it discovered, so a per-track instance would throw that
#: away and scrape Apple's frontend for a new one on every single track. It
#: owns no connection of its own — AsyncHttpClient picks the pool for the
#: running loop — so sharing it across loops is safe.
_apple_client: AppleMusicMetadataClient | None = None


def _get_apple_client() -> AppleMusicMetadataClient:
    global _apple_client
    if _apple_client is None:
        from .apple_music_metadata import AppleMusicMetadataClient as _Client

        _apple_client = _Client()
    return _apple_client


async def _fetch_apple_ttml_async(song_id: str, word_by_word: bool = True) -> str:
    """Apple's own lyrics for `song_id`, as LRC, or "" if unavailable.

    This is the direct path: Apple's TTML rather than a third party's
    flattened JSON of it. It carries the per-syllable timings, the backing
    vocals and — where Apple ships them — an official translation and
    romanisation, none of which survive the relay.

    It needs a subscriber's Media-User-Token, so for most installs this
    returns "" immediately and the relay below does the work. That is why
    it is tried first rather than instead: it costs one skipped call when
    unconfigured and gives strictly better lyrics when it is.
    """
    try:
        from .apple_ttml import ttml_to_lrc

        client = _get_apple_client()
        if not client.has_media_user_token:
            return ""
        ttml = await client.get_lyrics_ttml(song_id)
        if not ttml:
            return ""
        return ttml_to_lrc(
            ttml,
            word_by_word=word_by_word,
            translation=_env_flag("SPOTIFLAC_APPLE_LYRICS_TRANSLATION"),
            romanization=_env_flag("SPOTIFLAC_APPLE_LYRICS_PRONUNCIATION"),
        )
    except Exception as exc:
        logger.debug("[lyrics/apple] direct TTML: %s", exc)
        return ""


async def _fetch_apple_async(
    track_name: str,
    artist_name: str,
    duration_s: int,
    isrc: str = "",
    timeout: int = 7,
    word_by_word: bool = True,
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
            if _itunes_result_matches(res, track_name, artist_name, duration_s)
        ]
        if not scored:
            return ""
        best_result, _best_score = max(scored, key=lambda x: x[1])

        song_id = best_result.get("trackId")
        if not song_id:
            return ""

        # Apple first, the relay second: same catalogue, but the relay only
        # ever gives back words and line times.
        direct = await _fetch_apple_ttml_async(str(song_id), word_by_word)
        if direct:
            return direct

        r_lyr = await client.get(
            _PAXSENIX_APPLE,
            params={"id": str(song_id)},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if not r_lyr.is_success:
            return ""
        return _apple_payload_to_lrc(r_lyr.json(), word_by_word=word_by_word)
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


#: How far apart two recordings' lengths may be and still count as the same
#: recording. An exact title gets the wider margin — a remaster or another
#: master moves the length by a few seconds. A title that only *contains* the
#: other needs the lengths to agree closely: "San Valentino" contains
#: "Valentino" and is a different song.
_ITUNES_DURATION_TOLERANCE_EXACT_S = 10
_ITUNES_DURATION_TOLERANCE_PARTIAL_S = 5

#: Part of Apple's lyrics cache key. Answers cached before a result had to
#: match on both title and artist (_itunes_result_matches) include other
#: songs' lyrics; a new generation retires them instead of serving them for
#: another week. Bump it whenever the way the Apple song is chosen changes.
_APPLE_CACHE_GENERATION = "m3"


def _words(text: str) -> str:
    """Just the words, punctuation dropped: iTunes credits a track to
    "21 Savage, Offset & Metro Boomin", and the comma must not stop "21
    Savage" from being found in it."""
    return " ".join(re.findall(r"\w+", text))


def _contains_words(haystack: str, needle: str) -> bool:
    """Whole-word containment, so "nas" is not found inside "jonas"."""
    needle = _words(needle)
    return bool(needle) and f" {needle} " in f" {_words(haystack)} "


#: What separates the artists within one credit ("A, B & C feat. D").
_CREDIT_SEPARATORS = re.compile(
    r"\s*(?:,|&|;|\bfeat\.?(?=\s)|\bft\.?(?=\s)|\bfeaturing\b)\s*",
    re.IGNORECASE,
)


def _credits(artist_name: str) -> set[str]:
    """The artists one credit string names, each reduced to its words."""
    return {
        _words(normalize_loose_string(part))
        for part in _CREDIT_SEPARATORS.split(artist_name or "")
    } - {""}


def _itunes_result_matches(
    res: dict,
    track_name: str,
    artist_name: str,
    duration_s: int,
) -> bool:
    """Whether an iTunes search result is the track being tagged.

    _score_itunes_result() only ranks. It used to decide as well, through a
    threshold of 50 that a result reached on the title alone (50) or on the
    artist alone (60), and the lyrics of whatever won were embedded: Guns N'
    Roses' "November Rain" for Kris Wu's, another song off VillaBanks' album
    "Quanto Manca 2" for "Quanto Manca", Gigi D'Alessio's "San Valentino" for
    Vale Lambo's "Valentino". A result now has to match on both title and
    artist, and on length whenever both lengths are known.
    """
    wanted_track = normalize_loose_string(simplify_track_name(track_name))
    result_track = normalize_loose_string(
        simplify_track_name(res.get("trackName", "") or "")
    )
    wanted_artists = _credits(artist_name)
    result_artists = _credits(res.get("artistName", "") or "")
    if not (wanted_track and result_track and wanted_artists and result_artists):
        return False

    # Whole credits, not words: "Nas" is a word of "Lil Nas X" and a different
    # artist, while "21 Savage" is one of the credits of "21 Savage, Offset &
    # Metro Boomin".
    if not wanted_artists & result_artists:
        return False

    title_exact = _words(result_track) == _words(wanted_track)
    if not title_exact and not (
        _contains_words(result_track, wanted_track)
        or _contains_words(wanted_track, result_track)
    ):
        return False

    result_s = (res.get("trackTimeMillis") or 0) / 1000.0
    if duration_s > 0 and result_s > 0:
        tolerance = (
            _ITUNES_DURATION_TOLERANCE_EXACT_S
            if title_exact
            else _ITUNES_DURATION_TOLERANCE_PARTIAL_S
        )
        return abs(result_s - duration_s) <= tolerance
    # Nothing to check the length against: only an exact title is trusted.
    return title_exact


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
# Apple-TTML catalogues: BiniLyrics, Unison
# ---------------------------------------------------------------------------

_BINI_API = "https://lyrics-api.binimum.org/"
_UNISON_API = "https://unison.boidu.dev/lyrics"

#: A recording is only taken when its length is this close to the track's, so
#: the words fall on the same beat. Both catalogues search by name, and a live
#: or sped-up cut of the same song is a different take.
_TTML_LENGTH_SLACK_S = 4

#: Providers whose text depends on `apple_word_by_word`, and so cannot share a
#: cache entry between the two renderings.
_TTML_PROVIDERS = frozenset({"binilyrics", "unison"})


def _bini_lyrics_url(value: object) -> str:
    """`value` if it is an https link on Bini's own storage, else "".

    The search answers with the address of the TTML to fetch next. That
    address comes from a third party's response, so it is only followed
    when it stays on the host that serves the lyrics.
    """
    if not isinstance(value, str):
        return ""
    parsed = urllib.parse.urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host == "binimum.org" or host.endswith(".binimum.org")
    ):
        return ""
    return value


def _best_bini_result(
    results: object,
    duration_s: int,
    isrc: str,
) -> dict | None:
    """The best of what Bini's search found for the track.

    A result is taken when its ISRC is the track's own, or its length is
    within `_TTML_LENGTH_SLACK_S` of it. Among those, word timing beats line
    timing, then an ISRC match, then the closest length.
    """
    best: dict | None = None
    best_rank: tuple[bool, bool, int] | None = None
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or not _bini_lyrics_url(item.get("lyricsUrl")):
            continue
        same_isrc = bool(isrc) and str(item.get("isrc") or "").upper() == isrc.upper()
        length = item.get("duration")
        has_length = duration_s > 0 and isinstance(length, (int, float)) and length > 0
        off = abs(int(length) - duration_s) if has_length else 0
        if not same_isrc and not (has_length and off <= _TTML_LENGTH_SLACK_S):
            continue
        rank = (item.get("timing_type") != "word", not same_isrc, off)
        if best_rank is None or rank < best_rank:
            best, best_rank = item, rank
    return best


async def _fetch_bini_async(
    track_name: str,
    artist_name: str,
    album_name: str = "",
    duration_s: int = 0,
    isrc: str = "",
    word_by_word: bool = True,
    timeout: int = 7,
) -> str:
    """BiniLyrics: Apple Music's own TTML for over a million recordings.

    No key. The search answers with metadata and a link; the TTML itself is a
    second request, to a plain file host.
    """
    if not track_name or not artist_name:
        return ""
    params = {"track": track_name, "artist": artist_name}
    if duration_s > 0:
        params["duration"] = str(duration_s)
    if album_name:
        params["album"] = album_name
    try:
        from .apple_ttml import ttml_to_lrc

        client = await NetworkManager.get_async_client_safe()
        r = await client.get(_BINI_API, params=params, timeout=timeout)
        if r.status_code != 200:
            return ""
        payload = r.json()
        found = _best_bini_result(
            payload.get("results") if isinstance(payload, dict) else None,
            duration_s,
            isrc,
        )
        if not found:
            return ""
        ttml = await client.get(_bini_lyrics_url(found["lyricsUrl"]), timeout=timeout)
        if ttml.status_code != 200 or not ttml.text:
            return ""
        return ttml_to_lrc(ttml.text, word_by_word=word_by_word)
    except Exception as exc:
        logger.debug("[lyrics/binilyrics] async: %s", exc)
        return ""


async def _fetch_unison_async(
    track_name: str,
    artist_name: str,
    album_name: str = "",
    duration_s: int = 0,
    isrc: str = "",
    word_by_word: bool = True,
    timeout: int = 7,
) -> str:
    """Unison: lyrics people write by hand for Better Lyrics.

    A small corpus next to the rest, but what is in it is Apple Music's TTML
    at its best. Reads need no key; the signed write half of its API (submit,
    vote, report) is not touched. Only TTML and LRC are taken: its plain text
    carries nothing the other providers do not.
    """
    if not track_name or not artist_name:
        return ""
    params = {"song": track_name, "artist": artist_name}
    if duration_s > 0:
        params["duration"] = str(duration_s)
    if album_name:
        params["album"] = album_name
    try:
        from .apple_ttml import ttml_to_lrc

        client = await NetworkManager.get_async_client_safe()
        r = await client.get(_UNISON_API, params=params, timeout=timeout)
        if r.status_code != 200:
            return ""
        payload = r.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("lyrics"), str):
            return ""

        same_isrc = bool(isrc) and str(data.get("isrc") or "").upper() == isrc.upper()
        length = data.get("duration")
        length_ok = (
            duration_s > 0
            and isinstance(length, (int, float))
            and length > 0
            and abs(length - duration_s) <= _TTML_LENGTH_SLACK_S
        )
        if not same_isrc and not length_ok:
            return ""

        if data.get("format") == "ttml":
            return ttml_to_lrc(data["lyrics"], word_by_word=word_by_word)
        if data.get("format") == "lrc":
            return data["lyrics"]
        return ""
    except Exception as exc:
        logger.debug("[lyrics/unison] async: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# JioSaavn: Hindi and regional-Indian catalogue (Bollywood and beyond)
# ---------------------------------------------------------------------------

#: JioSaavn's own web player calls this same endpoint. The various
#: "unofficial JioSaavn API" projects on GitHub are just thin proxies in
#: front of it, meant to be self-hosted rather than shared publicly, so
#: going straight to the source avoids depending on someone else's uptime
#: for data JioSaavn already serves without a key.
_JIOSAAVN_API = "https://www.jiosaavn.com/api.php"
_JIOSAAVN_PARAMS = {
    "_format": "json",
    "_marker": "0",
    "api_version": "4",
    "ctx": "web6dot0",
}
_JIOSAAVN_LENGTH_SLACK_S = 4


def _best_jiosaavn_result(results: object, duration_s: int) -> dict | None:
    """The closest-length search hit that JioSaavn actually has lyrics for.

    Most of its catalogue has `has_lyrics: false` — a licensing flag, not a
    missing-data one — so that is filtered before length is even compared.
    """
    best: dict | None = None
    best_off = 0
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        more = item.get("more_info")
        if not isinstance(more, dict) or more.get("has_lyrics") != "true":
            continue
        try:
            length = int(more.get("duration") or 0)
        except (TypeError, ValueError):
            length = 0
        off = abs(length - duration_s) if duration_s > 0 and length > 0 else 0
        if duration_s > 0 and length > 0 and off > _JIOSAAVN_LENGTH_SLACK_S:
            continue
        if best is None or off < best_off:
            best, best_off = item, off
    return best


async def _fetch_jiosaavn_async(
    track_name: str,
    artist_name: str,
    duration_s: int = 0,
    timeout: int = 7,
) -> str:
    """JioSaavn: plain (unsynced) lyrics, strongest for Hindi and regional
    Indian languages — a catalogue the other providers barely cover.
    """
    if not track_name or not artist_name:
        return ""
    try:
        client = await NetworkManager.get_async_client_safe()
        search = await client.get(
            _JIOSAAVN_API,
            params={
                **_JIOSAAVN_PARAMS,
                "__call": "search.getResults",
                "q": f"{track_name} {artist_name}",
                "p": "1",
                "n": "10",
            },
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if not search.is_success:
            return ""
        payload = search.json()
        found = _best_jiosaavn_result(
            payload.get("results") if isinstance(payload, dict) else None,
            duration_s,
        )
        if not found:
            return ""
        lyrics = await client.get(
            _JIOSAAVN_API,
            params={
                **_JIOSAAVN_PARAMS,
                "__call": "lyrics.getLyrics",
                "lyrics_id": str(found["id"]),
            },
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if not lyrics.is_success:
            return ""
        data = lyrics.json()
        text = data.get("lyrics") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            return ""
        # Some JioSaavn responses carry line breaks as literal `<br>` tags
        # rather than `\n`; the other providers' output never does, so this
        # is normalized before it reaches the rest of the pipeline.
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        return html.unescape(text).strip()
    except Exception as exc:
        logger.debug("[lyrics/jiosaavn] async: %s", exc)
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
        word_by_word=ctx.apple_word_by_word,
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
    "binilyrics": lambda ctx: _fetch_bini_async(
        ctx.clean_track,
        ctx.clean_artist,
        ctx.album_name,
        ctx.duration_s,
        ctx.isrc,
        word_by_word=ctx.apple_word_by_word,
    ),
    "unison": lambda ctx: _fetch_unison_async(
        ctx.clean_track,
        ctx.clean_artist,
        ctx.album_name,
        ctx.duration_s,
        ctx.isrc,
        word_by_word=ctx.apple_word_by_word,
    ),
    "jiosaavn": lambda ctx: _fetch_jiosaavn_async(
        ctx.clean_track,
        ctx.clean_artist,
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
    apple_word_by_word: bool = True,
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

    # Apple's word-by-word and line-synced renderings are different text from
    # the same fetch, so they cannot share a cache entry — but only the
    # providers that read the setting need their key splitting (Apple, and the
    # TTML catalogues); every other provider ignores it.
    def _provider_track_key(provider_name: str) -> str:
        mode = "wbw" if apple_word_by_word else "line"
        if provider_name == "apple":
            return f"{track_key}|{mode}|{_APPLE_CACHE_GENERATION}"
        if provider_name in _TTML_PROVIDERS:
            return f"{track_key}|{mode}"
        return track_key

    def _cached_for(provider_name: str) -> str | None:
        key = f"{provider_name}|{_provider_track_key(provider_name)}"
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
        apple_word_by_word=apple_word_by_word,
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
            put_cached_response(
                "lyrics-provider",
                f"{provider_name}|{_provider_track_key(provider_name)}",
                text,
            )
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

            lyrics_text = lyrics.strip()
            if _env_flag("SPOTIFLAC_LYRICS_JELLYFIN_COMPAT"):
                lyrics_text = apply_jellyfin_word_gap(lyrics_text)

            result = (
                add_lrc_metadata(
                    lyrics_text,
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

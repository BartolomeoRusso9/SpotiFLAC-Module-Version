"""AppleMusicMetadataClient — retrieves track/album/artist/playlist metadata
via Apple Music's public AMP API.
"""

from __future__ import annotations

from .env import env_value

import asyncio
import base64
import json
import logging
import re
import time as _time
import unicodedata
import urllib.parse
import weakref
from typing import Any

from typing_extensions import Self

from mornye.core.errors import AuthError, ErrorKind, InvalidUrlError, MornyeError
from mornye.core.http import AsyncHttpClient
from mornye.core.models import TrackMetadata
from mornye.core.url_utils import url_host_matches

logger = logging.getLogger(__name__)

_APPLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_JWT_KNOWN_PREFIXES = (
    "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IldlYlBsYXlLaWQifQ.",
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiIsImtpZCI6IldlYlBsYXlLaWQifQ.",
)
_JWT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.",
)

#: Where to look for the anonymous developer token, in order. More than one
#: because Apple retires these paths without notice — /us/browse became a
#: redirect — and a single hard-coded entry page takes the whole provider
#: down with it.
_TOKEN_ENTRY_PAGES = (
    "https://music.apple.com/us/new",
    "https://music.apple.com/us/browse",
    "https://music.apple.com/us/listen-now",
)

_METADATA_CACHE_TTL_S = 5 * 60
_SEARCH_CACHE_TTL_S = 60
_CACHE_MAX_ITEMS = 500


def _extract_jwt_from_string(text: str) -> str | None:
    """Estrae un token JWT Apple Music da una stringa usando i prefissi noti
    (indexOf + scan carattere per carattere, come extractJWTFromString in index.js).
    """
    for prefix in _JWT_KNOWN_PREFIXES:
        idx = text.find(prefix)
        if idx == -1:
            continue
        end = idx
        while end < len(text) and text[end] in _JWT_CHARS:
            end += 1
        candidate = text[idx:end]
        parts = candidate.split(".")
        if len(parts) == 3 and all(parts):
            return candidate
    return None


#: attributes.audioTraits, best first. Apple lists every tier a release is
#: available in, so the entry that matters is the highest one present.
_AUDIO_TRAITS_RANK = (
    ("hi-res-lossless", "HI_RES_LOSSLESS"),
    ("lossless", "LOSSLESS"),
    ("atmos", "DOLBY_ATMOS"),
    ("spatial", "SPATIAL_AUDIO"),
    ("lossy-stereo", "LOSSY"),
)


def _quality_from_traits(traits: list[str]) -> str:
    """The best audio tier named in `audioTraits`, in this codebase's terms.

    Informational only. Apple does not serve the audio here — the catalogue
    API gives previews — so this records what the release *is*, which is
    what makes it useful when deciding whether a local copy is worth
    upgrading, not what was downloaded.
    """
    present = {t.strip().lower().replace("_", "-") for t in traits}
    for trait, name in _AUDIO_TRAITS_RANK:
        if trait in present:
            return name
    return ""


def _artwork_url(artwork: Any, size: int = 3000) -> str:
    """Expand Apple's ``{w}``/``{h}`` artwork template safely.

    The API has returned both ``{w}x{h}`` and separate placeholders over
    time.  Replacing the individual tokens handles both forms and also
    removes the last placeholder (``{f}``) found in some catalogue answers.
    """
    if not isinstance(artwork, dict):
        return ""
    url = str(artwork.get("url") or "").strip()
    if not url:
        return ""
    rendered = url.replace("{w}", str(size)).replace("{h}", str(size))
    return rendered.replace("{f}", "jpg")


def _unique_values(values: Any) -> list[str]:
    """Return trimmed, nonempty strings in order, deduplicated ignoring case."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _genre_string(*attrs: dict[str, Any]) -> str:
    """Join unique genres from the first populated source, omitting Music."""
    genres: list[str] = []
    for attr in attrs:
        genres.extend(str(value) for value in (attr.get("genreNames") or []))
        if genres:
            break
    return "; ".join(
        value for value in _unique_values(genres) if value.casefold() != "music"
    )


def _first_relationship_item(
    resource: dict[str, Any] | None, name: str
) -> dict[str, Any] | None:
    """Return the first related resource if it is a dictionary, else None."""
    relationship = (resource or {}).get("relationships", {}).get(name, {})
    items = relationship.get("data") or []
    return items[0] if items and isinstance(items[0], dict) else None


def _album_type_from_attrs(album_attr: dict[str, Any]) -> str:
    """The release kind, from the flags Apple sets on an album.

    Checked most specific first: a compilation single would otherwise be
    reported as a single, and "compilation" is the more useful of the two
    for anything deciding where the file belongs.
    """
    if not album_attr:
        return ""
    if album_attr.get("isCompilation"):
        return "compilation"
    if album_attr.get("isSingle"):
        return "single"
    if album_attr.get("name"):
        return "album"
    return ""


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def is_apple_music_url(url: str) -> bool:
    return url_host_matches(url, "music.apple.com")


def parse_apple_music_url(url: str) -> dict[str, str]:
    """Parses an Apple Music URL and returns type, id, and storefront."""
    url = (url or "").strip()

    m = re.search(
        r"music\.apple\.com/([a-z]{2})/(album|playlist|artist|song)/[^/]*/([a-zA-Z0-9.]+)",
        url,
        re.IGNORECASE,
    )
    if not m:
        msg = f"Apple Music URL not recognized: {url}"
        raise InvalidUrlError(msg)

    storefront = m.group(1).lower()
    kind = m.group(2).lower()
    entity_id = m.group(3)

    song_m = re.search(r"[?&]i=(\d+)", url)
    if kind == "album" and song_m:
        return {"type": "track", "id": song_m.group(1), "storefront": storefront}
    if kind == "song":
        return {"type": "track", "id": entity_id, "storefront": storefront}
    if kind == "album":
        return {"type": "album", "id": entity_id, "storefront": storefront}
    if kind == "playlist":
        return {"type": "playlist", "id": entity_id, "storefront": storefront}
    if kind == "artist":
        return {"type": "artist", "id": entity_id, "storefront": storefront}

    raise InvalidUrlError(url)


# ---------------------------------------------------------------------------
# Helper normalization
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
    def __init__(
        self,
        timeout_s: int = 15,
        media_user_token: str | None = None,
        storefront: str | None = None,
    ) -> None:
        """Configure HTTP access, optional subscriber credentials, and local caches."""
        self._timeout = timeout_s
        # The anonymous developer token opens the catalogue; the lyrics
        # endpoints additionally want a *subscriber*, which is what the
        # Media-User-Token identifies. It belongs to the user, so it is
        # never fetched or guessed — supplied, or the feature is off.
        self._media_user_token = (
            media_user_token
            if media_user_token is not None
            else env_value("MORNYE_APPLE_MEDIA_USER_TOKEN", "")
        ).strip()
        self._storefront = (
            storefront
            if storefront is not None
            else env_value("MORNYE_APPLE_STOREFRONT", "us")
        ).strip().lower() or "us"
        self._http = AsyncHttpClient(
            provider="apple_metadata",
            timeout_s=timeout_s,
            headers={
                "User-Agent": _APPLE_UA,
                "Accept": "application/json",
                "Origin": "https://music.apple.com",
                "Referer": "https://music.apple.com/",
            },
        )
        self._auth_token: str | None = None
        self._token_expiry: float = 0.0  # timestamp Unix; 0 = mai valido
        self._token_locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = weakref.WeakKeyDictionary()
        # The JS extension keeps a bounded, short-lived cache.  Keep it on
        # the Python client as well: album hydration is deliberately richer
        # than a single song request and must not multiply API traffic for a
        # playlist or an artist discography.
        self._cache: dict[str, tuple[float, Any]] = {}

    def _cache_get(self, key: str) -> Any:
        """Return a cached value, or None if absent or expired; remove expired entries."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if _time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any, ttl_s: float) -> Any:
        """Cache and return a value, evicting the earliest expiry at capacity."""
        if len(self._cache) >= _CACHE_MAX_ITEMS:
            oldest_key = min(self._cache, key=lambda item: self._cache[item][0])
            self._cache.pop(oldest_key, None)
        self._cache[key] = (_time.monotonic() + ttl_s, value)
        return value

    @staticmethod
    def _normalize_storefront(storefront: str | None, default: str) -> str:
        """Normalize a two-letter storefront code, falling back to us if invalid."""
        value = (storefront or default or "us").strip().lower()
        return value if re.fullmatch(r"[a-z]{2}", value) else "us"

    @property
    def has_media_user_token(self) -> bool:
        """Whether the subscriber-only endpoints can be called at all."""
        return bool(self._media_user_token)

    @property
    def storefront(self) -> str:
        return self._storefront

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass  # The HTTP client's lifecycle is managed by NetworkManager

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    def _parse_token_expiry(self, token: str) -> None:
        """Reads the `exp` field from the JWT payload and sets the internal expiry."""
        try:
            payload_b64 = token.split(".")[1]
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            if "exp" in payload:
                self._token_expiry = float(payload["exp"]) - 300.0
            else:
                self._token_expiry = _time.time() + 43200.0
        except Exception:
            self._token_expiry = _time.time() + 43200.0

    def _token_lock(self) -> asyncio.Lock:
        """The token-refresh lock belonging to the running loop.

        Per loop rather than per client, for the same reason
        NetworkManager keeps its clients that way: a lock created under one
        event loop cannot be awaited from another, and one client instance
        can be shared by everything running on any of them.
        """
        loop = asyncio.get_running_loop()
        lock = self._token_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._token_locks[loop] = lock
        return lock

    async def _get_token(self) -> str:
        """The developer token, discovered once and reused until it expires.

        Held behind a lock: a batch of lyric lookups starts as a burst of
        concurrent requests with no token yet, and without it every one of
        them would scrape the web frontend for the same JWT.
        """
        if self._auth_token and _time.time() < self._token_expiry:
            return self._auth_token

        async with self._token_lock():
            # Whoever held the lock has usually just fetched one.
            if self._auth_token and _time.time() < self._token_expiry:
                return self._auth_token
            return await self._discover_token()

    async def _discover_token(self) -> str:
        """Extracts the anonymous JWT token from the web frontend using 3 strategies:
        1. devToken=JWT in the HTML source
        2. Known JWT prefixes in the HTML
        3. The page's JS bundles (skipping legacy ones).
        """
        try:
            # follow_redirects, and more than one entry page: /us/browse now
            # answers 301, and without following it the client raised
            # "HTTP 301" before it ever looked for a token — every Apple
            # Music lookup failed at the first request.
            html = ""
            for entry in _TOKEN_ENTRY_PAGES:
                try:
                    res = await self._http.get(
                        entry,
                        timeout=self._timeout,
                        follow_redirects=True,
                    )
                except Exception as exc:
                    logger.debug("[apple_metadata] %s unusable: %s", entry, exc)
                    continue
                if res.text:
                    html = res.text
                    break
            if not html:
                raise MornyeError(
                    ErrorKind.NETWORK_ERROR,
                    "Apple Music web frontend unreachable for token extraction.",
                )
            unquoted_html = urllib.parse.unquote(html)

            # Strategia 1: devToken=JWT nel parametro URL
            m = re.search(
                r"devToken=([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                html,
            )
            if m:
                token = m.group(1)
                self._auth_token = token
                self._parse_token_expiry(token)
                return token

            # Strategia 2: Prefissi JWT noti nell'HTML
            token = _extract_jwt_from_string(unquoted_html)
            if token:
                self._auth_token = token
                self._parse_token_expiry(token)
                return token

            # Strategia 3: Bundle JS (salto quelli legacy)
            js_scripts = re.findall(r'src="(/assets/index[^"]*\.js)"', html)
            if not js_scripts:
                js_scripts = re.findall(r'src="(/assets/[^"]*\.js)"', html)

            for src in js_scripts[:6]:
                if "-legacy" in src:
                    continue
                js_url = "https://music.apple.com" + src
                try:
                    js_res = await self._http.get(
                        js_url,
                        timeout=self._timeout,
                        follow_redirects=True,
                    )
                    token = _extract_jwt_from_string(urllib.parse.unquote(js_res.text))
                    if token:
                        logger.debug(
                            "[apple_metadata] Token found in JS bundle: %s",
                            src,
                        )
                        self._auth_token = token
                        self._parse_token_expiry(token)
                        return token
                except Exception:
                    continue

            raise MornyeError(
                ErrorKind.NETWORK_ERROR,
                "JWT token not found in HTML or JS bundles.",
            )

        except MornyeError:
            raise
        except Exception as e:
            logger.exception("[apple_metadata] Unable to retrieve JWT token: %s", e)
            raise MornyeError(
                ErrorKind.NETWORK_ERROR,
                f"Unable to retrieve Apple Music token: {e}",
            )

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        _media_user_token: str = "",
    ) -> dict[str, Any]:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        if _media_user_token:
            # Only sent where it is needed. The catalogue endpoints work
            # anonymously, and attaching a user identity to them would tie
            # ordinary metadata reads to the user's Apple account for no
            # gain.
            headers["Media-User-Token"] = _media_user_token

        url = (
            path
            if path.startswith("https://")
            else f"https://amp-api.music.apple.com/v1/catalog/{path.lstrip('/')}"
        )

        try:
            resp = await self._http.get(
                url,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
            return resp.json()
        except AuthError:
            # Token scaduto: forza rinnovo e riprova una volta
            self._auth_token = None
            self._token_expiry = 0.0
            token = await self._get_token()
            # Only the Authorization header is stale: rebuilding the dict
            # from scratch would drop the caller's Media-User-Token and turn
            # the retry into an anonymous request (no user lyrics).
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._http.get(
                url,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
            return resp.json()

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def _pagete_tracks(
        self,
        initial_items: list[dict[str, Any]],
        first_next: str | None,
        label: str = "resource",
    ) -> list[dict[str, Any]]:
        """Completes the track list by following subsequent `next` links."""
        items = list(initial_items)
        next_path = first_next

        while next_path:
            try:
                page = await self._get(f"https://amp-api.music.apple.com{next_path}")
                page_items = page.get("data", [])
                if not page_items:
                    break
                items.extend(page_items)
                next_path = page.get("next")
                await asyncio.sleep(0.3)
            except Exception as exc:
                logger.warning(
                    "[apple_metadata] Track pagination %s interrupted: %s",
                    label,
                    exc,
                )
                break

        return items

    async def _pagete_relationship(self, initial_path: str) -> list[dict[str, Any]]:
        """Iterates a standalone relationship (e.g. /artists/{id}/albums) following `next`."""
        results: list[dict[str, Any]] = []
        next_url: str | None = initial_path

        while next_url:
            try:
                data = await self._get(next_url)
            except Exception as exc:
                logger.warning("[apple_metadata] Pagination interrupted: %s", exc)
                break

            page = data.get("data", [])
            results.extend(page)

            raw_next = data.get("next")
            if not raw_next or not page:
                break

            next_url = f"https://amp-api.music.apple.com{raw_next}"
            await asyncio.sleep(0.3)

        return results

    # ------------------------------------------------------------------
    # Metodi di fetching
    # ------------------------------------------------------------------

    @staticmethod
    def _album_id_from_song(song: dict[str, Any]) -> str:
        """Extract the album ID from relationships, attributes, or the song URL."""
        relation = song.get("relationships", {}).get("albums", {})
        items = relation.get("data") or []
        if items and isinstance(items[0], dict) and items[0].get("id"):
            return str(items[0]["id"])
        attr = song.get("attributes", {})
        for key in ("albumId", "albumID"):
            if attr.get(key):
                return str(attr[key])
        match = re.search(r"/album/[^/]+/(\d+)", str(attr.get("url") or ""))
        return match.group(1) if match else ""

    async def _hydrate_album(
        self,
        album_id: str,
        storefront: str,
        *,
        include_tracks: bool = True,
    ) -> dict[str, Any] | None:
        """Load the complete album resource used by the JS extension.

        Song relationships often contain only the album id and name.  The
        second request is what supplies record label, copyright, UPC, album
        flags, canonical URLs and the real disc count.
        """
        if not album_id:
            return None
        cache_key = f"raw-album:{storefront}:{album_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        include = "tracks,artists" if include_tracks else "artists"
        try:
            data = await self._get(
                f"/{storefront}/albums/{album_id}",
                {
                    "include": include,
                    "extend": "artistUrl,editorialArtwork,trackCount,upc",
                },
            )
        except Exception as exc:
            logger.debug(
                "[apple_metadata] album hydration %s failed: %s", album_id, exc
            )
            return None
        albums = data.get("data") or []
        if not albums:
            return None
        album = albums[0]
        if include_tracks:
            track_items = await self._pagete_tracks(
                (album.get("relationships", {}).get("tracks", {}) or {}).get(
                    "data", []
                ),
                (album.get("relationships", {}).get("tracks", {}) or {}).get("next"),
                label=f"album {album_id}",
            )
            album = dict(album)
            album["_totalDiscs"] = max(
                (
                    int((track.get("attributes") or {}).get("discNumber") or 0)
                    for track in track_items
                ),
                default=1,
            )
        return self._cache_set(cache_key, album, _METADATA_CACHE_TTL_S)

    async def _hydrate_albums_for_songs(
        self,
        songs: list[dict[str, Any]],
        storefront: str,
    ) -> dict[str, dict[str, Any]]:
        """Hydrate unique album ids in small concurrent batches.

        The extension uses one ``albums?ids=...`` call for up to 25 albums;
        the Python API client has no batch endpoint abstraction, so bounded
        individual requests preserve the same behavior without flooding the
        shared HTTP pool.
        """
        ids = _unique_values(self._album_id_from_song(song) for song in songs)
        if not ids:
            return {}
        semaphore = asyncio.Semaphore(5)

        async def load(album_id: str) -> tuple[str, dict[str, Any] | None]:
            """Hydrate one album while respecting the shared concurrency limit."""
            async with semaphore:
                return album_id, await self._hydrate_album(
                    album_id, storefront, include_tracks=False
                )

        loaded = await asyncio.gather(*(load(album_id) for album_id in ids))
        return {album_id: album for album_id, album in loaded if album is not None}

    async def get_track(self, track_id: str, storefront: str = "us") -> TrackMetadata:
        """Return cached or fetched track metadata enriched with album details.

        Raise MornyeError with TRACK_NOT_FOUND when the song is absent.
        """
        storefront = self._normalize_storefront(storefront, self._storefront)
        cache_key = f"track:{storefront}:{track_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        data = await self._get(
            f"/{storefront}/songs/{track_id}",
            {
                "include": "albums,artists,composers,genres",
                "extend": "artistUrl,editorialArtwork,trackCount,upc",
            },
        )
        results = data.get("data", [])
        if not results:
            raise MornyeError(
                ErrorKind.TRACK_NOT_FOUND,
                f"Track {track_id} not found.",
            )
        song = results[0]
        album = await self._hydrate_album(
            self._album_id_from_song(song), storefront, include_tracks=True
        )
        return self._cache_set(
            cache_key,
            self._parse_item(song, album or _first_relationship_item(song, "albums")),
            _METADATA_CACHE_TTL_S,
        )

    async def get_album_tracks(
        self,
        album_id: str,
        storefront: str = "us",
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        """Return the album resource and all paginated tracks with disc totals.

        Cache the result and raise TRACK_NOT_FOUND when the album is absent.
        """
        storefront = self._normalize_storefront(storefront, self._storefront)
        cache_key = f"album:{storefront}:{album_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        data = await self._get(
            f"/{storefront}/albums/{album_id}",
            {
                "include": "tracks,artists",
                "extend": "artistUrl,editorialArtwork,editorialVideo,trackCount,upc",
            },
        )
        results = data.get("data", [])
        if not results:
            raise MornyeError(
                ErrorKind.TRACK_NOT_FOUND,
                f"Album {album_id} not found.",
            )

        album_data = results[0]
        tracks_rel = album_data.get("relationships", {}).get("tracks", {})

        tracks_items = await self._pagete_tracks(
            initial_items=tracks_rel.get("data", []),
            first_next=tracks_rel.get("next"),
            label=f"album {album_id}",
        )

        tracks = [self._parse_item(item, album_data) for item in tracks_items]

        # The disc count exists nowhere in the album's own attributes — it is
        # only visible as the highest discNumber across the track list, which
        # we have here and _parse_item does not. Without this every track of
        # a two-disc release was tagged DISCTOTAL=1.
        total_discs = max((track.disc_number for track in tracks), default=1)
        if total_discs > 1:
            tracks = [
                track.model_copy(update={"total_discs": total_discs})
                for track in tracks
            ]

        album_attr = album_data.get("attributes", {})
        artwork_url = _artwork_url(album_attr.get("artwork"))
        release_date = album_attr.get("releaseDate", "").split("T")[0]

        # Keep the complete Apple resource.  The previous reduced dict lost
        # label, copyright, genre, UPC, album flags and the canonical URL,
        # even though the extension exposes all of them to metadata users.
        album_result = dict(album_data)
        album_result["attributes"] = dict(album_attr)
        album_result["attributes"]["releaseDate"] = release_date
        album_result["attributes"]["trackCount"] = len(tracks)
        album_result["attributes"]["artwork"] = {"url": artwork_url}
        result = (album_result, tracks)
        return self._cache_set(cache_key, result, _METADATA_CACHE_TTL_S)

    async def get_playlist_tracks(
        self,
        playlist_id: str,
        storefront: str = "us",
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        """Return a playlist resource and its songs enriched with album metadata.

        Cache the result and raise TRACK_NOT_FOUND when the playlist is absent.
        """
        storefront = self._normalize_storefront(storefront, self._storefront)
        cache_key = f"playlist:{storefront}:{playlist_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        data = await self._get(
            f"/{storefront}/playlists/{playlist_id}",
            {
                "include": "tracks",
                "extend": "editorialArtwork,editorialVideo",
            },
        )
        results = data.get("data", [])
        if not results:
            raise MornyeError(
                ErrorKind.TRACK_NOT_FOUND,
                f"Playlist {playlist_id} not found.",
            )

        playlist_data = results[0]
        tracks_rel = playlist_data.get("relationships", {}).get("tracks", {})

        tracks_items = await self._pagete_tracks(
            initial_items=tracks_rel.get("data", []),
            first_next=tracks_rel.get("next"),
            label=f"playlist {playlist_id}",
        )

        songs = [item for item in tracks_items if item.get("type") in ("songs", "")]
        albums = await self._hydrate_albums_for_songs(songs, storefront)
        tracks = [
            self._parse_item(item, albums.get(self._album_id_from_song(item)))
            for item in songs
        ]
        result = (playlist_data, tracks)
        return self._cache_set(cache_key, result, _METADATA_CACHE_TTL_S)

    async def get_artist_albums(
        self,
        artist_id: str,
        include_featuring: bool = True,
        storefront: str = "us",
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        artist_data = await self._get(f"/{storefront}/artists/{artist_id}")
        artist_results = artist_data.get("data", [])
        if not artist_results:
            raise MornyeError(
                ErrorKind.TRACK_NOT_FOUND,
                f"Artist {artist_id} not found.",
            )

        artist_obj = artist_results[0]
        artist_name = artist_obj.get("attributes", {}).get("name", "Unknown")

        album_ids: list[str] = []
        seen_ids: set[str] = set()

        for album_data in await self._pagete_relationship(
            f"/{storefront}/artists/{artist_id}/albums",
        ):
            aid = str(album_data.get("id", ""))
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                album_ids.append(aid)

        own_album_ids: set[str] = set(album_ids)

        if include_featuring:
            for album_data in await self._pagete_relationship(
                f"/{storefront}/artists/{artist_id}/appears-on-albums",
            ):
                aid = str(album_data.get("id", ""))
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    album_ids.append(aid)

        logger.info(
            "[apple_metadata] %s: %d album totali da scaricare",
            artist_name,
            len(album_ids),
        )

        # Fetch parallelo con asyncio.gather + semaphore for concurrency limiting
        semaphore = asyncio.Semaphore(5)

        async def _fetch_one(
            aid: str,
        ) -> tuple[str, list[TrackMetadata] | None]:
            async with semaphore:
                try:
                    _, album_tracks = await self.get_album_tracks(
                        aid,
                        storefront=storefront,
                    )
                    return aid, album_tracks
                except Exception as exc:
                    logger.warning("[apple_metadata] Album %s skipped: %s", aid, exc)
                    return aid, None

        raw_results = await asyncio.gather(*[_fetch_one(aid) for aid in album_ids])

        results_dict: dict[str, list[TrackMetadata]] = {
            aid: tracks for aid, tracks in raw_results if tracks is not None
        }

        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()

        for aid in album_ids:
            if aid not in results_dict:
                continue
            for track in results_dict[aid]:
                if track.isrc and track.isrc in seen_isrc:
                    continue
                if include_featuring and aid not in own_album_ids:
                    if not _artist_in_track(artist_name, track.artists):
                        continue
                if track.isrc:
                    seen_isrc.add(track.isrc)
                tracks.append(track)

        return artist_obj, tracks

    # ------------------------------------------------------------------
    # Entry point pubblico
    # ------------------------------------------------------------------

    async def get_url(
        self,
        url: str,
        include_featuring: bool = True,
    ) -> tuple[str, list[TrackMetadata], str, dict[str, Any]]:
        """Resolve an Apple URL to its title, tracks, artwork URL, and album info."""
        info = parse_apple_music_url(url)
        t = info["type"]
        storefront = info.get("storefront", "us")

        if t == "track":
            meta = await self.get_track(info["id"], storefront=storefront)
            return meta.title, [meta], meta.cover_url, {}

        if t == "album":
            album, tracks = await self.get_album_tracks(
                info["id"],
                storefront=storefront,
            )
            name = album.get("attributes", {}).get("name", "Unknown Album")
            release_date = album.get("attributes", {}).get("releaseDate", "")
            artwork_url = _artwork_url(album.get("attributes", {}).get("artwork"))
            album_meta = {"release_date": release_date, "track_count": len(tracks)}
            return name, tracks, artwork_url, album_meta

        if t == "playlist":
            playlist, tracks = await self.get_playlist_tracks(
                info["id"],
                storefront=storefront,
            )
            name = playlist.get("attributes", {}).get("name", "Unknown Playlist")
            artwork_url = _artwork_url(playlist.get("attributes", {}).get("artwork"))
            return name, tracks, artwork_url, {}

        if t == "artist":
            artist, tracks = await self.get_artist_albums(
                info["id"],
                include_featuring=include_featuring,
                storefront=storefront,
            )
            name = artist.get("attributes", {}).get("name", "Unknown Artist")
            artwork_url = _artwork_url(artist.get("attributes", {}).get("artwork"))
            return name, tracks, artwork_url, {}

        raise MornyeError(
            ErrorKind.INVALID_URL,
            f"Apple Music type not supported: {t} (supportati: track, album, playlist, artist)",
        )

    async def search_async(
        self,
        query: str,
        limit: int = 20,
        storefront: str = "",
        kind: str | None = None,
    ) -> dict[str, list[Any]]:
        """Search songs, albums, artists and playlists like the extension.

        ``kind`` accepts ``track(s)``, ``album(s)``, ``artist(s)`` or
        ``playlist(s)``.  The default keeps the extension's mixed result
        shape while avoiding album hydration for every search hit.
        """
        query = str(query or "").strip()
        empty = {"tracks": [], "albums": [], "artists": [], "playlists": []}
        if not query:
            return empty
        store = self._normalize_storefront(storefront, self._storefront)
        normalized_limit = max(1, min(int(limit or 20), 25))
        type_map = {
            "track": "songs",
            "tracks": "songs",
            "album": "albums",
            "albums": "albums",
            "artist": "artists",
            "artists": "artists",
            "playlist": "playlists",
            "playlists": "playlists",
        }
        api_types = type_map.get(kind or "", "songs,albums,artists,playlists")
        cache_key = f"search:{store}:{query.casefold()}:{api_types}:{normalized_limit}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        data = await self._get(
            f"/{store}/search",
            {
                "term": query,
                "types": api_types,
                "limit": normalized_limit,
                "offset": 0,
            },
        )
        result = {key: [] for key in empty}
        raw = data.get("results") or {}
        songs = (raw.get("songs") or {}).get("data") or []
        if kind in (None, "track", "tracks"):
            albums_by_id = await self._hydrate_albums_for_songs(songs, store)
            result["tracks"] = [
                self._parse_item(song, albums_by_id.get(self._album_id_from_song(song)))
                for song in songs[:normalized_limit]
            ]

        for api_key, output_key, item_limit in (
            ("albums", "albums", normalized_limit if kind else 5),
            ("artists", "artists", normalized_limit if kind else 2),
            ("playlists", "playlists", normalized_limit if kind else 4),
        ):
            if kind and api_key != api_types:
                continue
            values = (raw.get(api_key) or {}).get("data") or []
            result[output_key] = [
                self._format_search_resource(item, output_key)
                for item in values[:item_limit]
            ]
        return self._cache_set(cache_key, result, _SEARCH_CACHE_TTL_S)

    async def search_by_type_async(
        self,
        query: str,
        kind: str,
        limit: int = 20,
        storefront: str = "",
    ) -> list[Any]:
        """Return search results for one supported kind, raising ValueError otherwise."""
        key = "tracks" if kind in ("track", "tracks") else f"{kind}s"
        if key not in {"tracks", "albums", "artists", "playlists"}:
            raise ValueError("kind must be track, album, artist or playlist")
        return (await self.search_async(query, limit, storefront, kind)).get(key, [])

    @staticmethod
    def _format_search_resource(item: dict[str, Any], kind: str) -> dict[str, Any]:
        """Convert an Apple search resource to the shared discovery result fields."""
        attr = item.get("attributes") or {}
        artwork = _artwork_url(attr.get("artwork"))
        value: dict[str, Any] = {
            "id": str(item.get("id") or ""),
            "name": attr.get("name") or "",
            "cover_url": artwork,
            "images": artwork,
            "external_url": attr.get("url") or "",
            "external_urls": attr.get("url") or "",
            "provider_id": "apple-music",
            "item_type": kind[:-1] if kind.endswith("s") else kind,
        }
        if kind == "albums":
            value.update(
                artists=attr.get("artistName") or "",
                release_date=attr.get("releaseDate") or "",
                total_tracks=attr.get("trackCount") or 0,
                album_type=_album_type_from_attrs(attr),
                label=attr.get("recordLabel") or "",
                copyright=attr.get("copyright") or "",
                genre=_genre_string(attr),
            )
        elif kind == "artists":
            value["artist_url"] = attr.get("url") or ""
            value["image_url"] = artwork
        elif kind == "playlists":
            description = attr.get("description") or {}
            value.update(
                owner=attr.get("curatorName") or "",
                description=re.sub(
                    r"<[^>]+>",
                    "",
                    str(description.get("standard") or description.get("short") or ""),
                ),
            )
        return value

    # ------------------------------------------------------------------
    # Lyrics (subscriber-only)
    # ------------------------------------------------------------------

    async def get_lyrics_ttml(
        self,
        song_id: str,
        storefront: str = "",
        syllable: bool = True,
    ) -> str:
        """The song's lyrics as raw TTML, or "" when unavailable.

        Two endpoints, tried in that order: `syllable-lyrics` carries a
        time for every syllable and is what word-by-word display needs;
        `lyrics` is the same words timed per line. Not every track has the
        syllable version — Apple rolls it out per catalogue — so falling
        back is the normal case, not the error case.

        Returns "" rather than raising for the expected refusals (no token,
        401/403 from an expired one, 404 for a track with no lyrics),
        because the caller's next move is the same in all of them: try
        another lyrics provider.
        """
        if not self._media_user_token:
            logger.debug(
                "[apple_metadata] no Media-User-Token configured; direct "
                "lyrics are unavailable (set MORNYE_APPLE_MEDIA_USER_TOKEN)",
            )
            return ""
        if not song_id:
            return ""

        store = (storefront or self._storefront).lower()
        paths = ["syllable-lyrics", "lyrics"] if syllable else ["lyrics"]
        for path in paths:
            try:
                data = await self._get(
                    f"/{store}/songs/{song_id}/{path}",
                    _media_user_token=self._media_user_token,
                )
            except MornyeError as exc:
                logger.debug("[apple_metadata] %s for %s: %s", path, song_id, exc)
                continue
            for entry in data.get("data") or []:
                ttml = (entry.get("attributes") or {}).get("ttml") or ""
                if ttml:
                    return ttml
        return ""

    # ------------------------------------------------------------------
    # Conversione dati API → TrackMetadata
    # ------------------------------------------------------------------

    def _parse_item(
        self,
        item: dict[str, Any],
        parent_album: dict[str, Any] | None = None,
    ) -> TrackMetadata:
        """Build track metadata from a song and available album relationships."""
        attr = item.get("attributes") or {}
        album = parent_album or _first_relationship_item(item, "albums") or {}
        album_attr = album.get("attributes") or {}

        cover_url = _artwork_url(attr.get("artwork")) or _artwork_url(
            album_attr.get("artwork")
        )

        release_value = attr.get("releaseDate") or album_attr.get("releaseDate") or ""
        release_date = str(release_value).split("T", 1)[0]

        genre = _genre_string(attr, album_attr)

        artist_items = (
            item.get("relationships", {}).get("artists", {}).get("data") or []
        )
        if not artist_items:
            artist_items = (
                album.get("relationships", {}).get("artists", {}).get("data") or []
            )
        artist_names = _unique_values(
            (artist.get("attributes") or {}).get("name")
            for artist in artist_items
            if isinstance(artist, dict)
        )
        artists = str(attr.get("artistName") or ", ".join(artist_names) or "Unknown")
        album_artist = str(
            attr.get("albumArtistName")
            or album_attr.get("artistName")
            or ", ".join(artist_names)
            or artists
        )
        artist_resource = artist_items[0] if artist_items else {}
        artist_id = str(artist_resource.get("id") or "")
        artist_attr = artist_resource.get("attributes") or {}
        album_id = str(album.get("id") or self._album_id_from_song(item) or "")
        album_url = str(album_attr.get("url") or "")
        if not album_url and album_id:
            album_url = f"https://music.apple.com/{self._normalize_storefront(None, self._storefront)}/album/{album_id}"
        track_url = str(attr.get("url") or "")
        if not track_url and item.get("id"):
            track_url = f"https://music.apple.com/{self._normalize_storefront(None, self._storefront)}/song/{item['id']}"

        # A preview is the only stream the catalogue API hands out without
        # a subscription, and it is what the rest of the pipeline uses to
        # fingerprint or audition a track.
        previews = attr.get("previews") or []
        preview_url = ""
        if previews and isinstance(previews[0], dict):
            preview_url = previews[0].get("url", "")

        # Apple states the rating as a word, and only on the tracks that
        # carry it — the album's own "explicit" means *some* track is, so
        # falling back to it would mark every clean track on the record
        # explicit too. Verified on The Dark Side of the Moon, where the
        # album is rated explicit and eight of its ten tracks are not.
        rating = attr.get("contentRating") or ""

        extra_info: dict[str, Any] = {}
        traits = [str(t) for t in (attr.get("audioTraits") or [])]
        if traits:
            extra_info["apple_audio_traits"] = traits
            quality = _quality_from_traits(traits)
            if quality:
                extra_info["apple_audio_quality"] = quality
        if attr.get("hasLyrics"):
            extra_info["apple_has_lyrics"] = True
        if attr.get("hasTimeSyncedLyrics"):
            extra_info["apple_has_time_synced_lyrics"] = True
        modes = []
        normalized_traits = {
            trait.strip().lower().replace("_", "-") for trait in traits
        }
        if "atmos" in normalized_traits:
            modes.append("DOLBY_ATMOS")
        if "spatial" in normalized_traits:
            modes.append("SPATIAL_AUDIO")
        if modes:
            extra_info["apple_audio_modes"] = modes

        return TrackMetadata(
            id=f"apple_{item.get('id', '')}",
            title=attr.get("name", "Unknown"),
            artists=artists,
            album=attr.get("albumName", album_attr.get("name", "Unknown")),
            album_artist=album_artist,
            isrc=attr.get("isrc", ""),
            track_number=attr.get("trackNumber", 1),
            disc_number=attr.get("discNumber", 1),
            total_tracks=int(album_attr.get("trackCount") or 0),
            total_discs=int(album.get("_totalDiscs") or 1),
            duration_ms=attr.get("durationInMillis", 0),
            release_date=release_date,
            cover_url=cover_url,
            external_url=track_url,
            genre=genre,
            # `publisher`, not `label`: TrackMetadata has no `label` field and
            # pydantic drops unknown keyword arguments without complaint, so
            # the record label Apple returns was being thrown away here.
            publisher=album_attr.get("recordLabel", ""),
            copyright=album_attr.get("copyright", ""),
            composer=attr.get("composerName", ""),
            upc=album_attr.get("upc", ""),
            preview_url=preview_url,
            album_type=_album_type_from_attrs(album_attr),
            is_explicit=rating == "explicit",
            album_id=album_id,
            album_url=album_url,
            artist_id=artist_id,
            artist_url=str(artist_attr.get("url") or ""),
            artist_names=artist_names,
            album_artist_names=_unique_values(
                (artist.get("attributes") or {}).get("name")
                for artist in (
                    album.get("relationships", {}).get("artists", {}).get("data") or []
                )
                if isinstance(artist, dict)
            ),
            extra_info=extra_info,
        )

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from SpotiFLAC.core.errors import ErrorKind, InvalidUrlError, SpotiflacError
from SpotiFLAC.core.http import AsyncHttpClient
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.core.spotfetch import SpotifyWebClient

logger = logging.getLogger(__name__)


def _run_async_sync(func, *args, **kwargs):
    """Thin alias for loop_runner.run_sync() — see that function for why the
    old three-branch shim (which created a fresh loop in every branch) went
    away. Kept as a name so the many call sites below read unchanged.
    """
    from .loop_runner import run_sync

    return run_sync(func(*args, **kwargs))


_FEATURING_GROUPS = frozenset({"appears_on", "compilation"})
_DISCOGRAPHY_SUBTYPES = frozenset(
    {"all", "album", "single", "compilation", "appears_on"},
)


@dataclass(frozen=True)
class ArtistSimple:
    """Artist con ID e URL esterno, per uso downstream."""

    id: str
    name: str
    external_url: str


# ---------------------------------------------------------------------------
# Internal helpers — avoid repetition across the client's methods
# ---------------------------------------------------------------------------


def _dig(node: Any, *keys: str) -> dict:
    """Walks a chain of keys, treating a null value like a missing one.

    `data.get("data", {}).get("trackUnion", {})` looks defensive but is not:
    the default only applies when the key is *absent*, and Spotify's GraphQL
    sends `{"data": null, "errors": [...]}` when a query partly fails — the
    key is present and null, so the chain reaches `None.get(...)` and raises
    `'NoneType' object has no attribute 'get'`. Under a big batch of
    single-track lookups that surfaced as the occasional "Metadata fetch
    failed" with no useful detail. Anything that isn't a dict — null, a
    list, a scalar — yields {} here, so callers can keep reading with .get().
    """
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _iso_date(node: Any) -> str:
    """The release date of an album node, which Spotify may send as null.

    A track the account cannot play — country-restricted, taken down — comes
    back with the shape intact but the fields emptied: `"date": null`,
    `"name": ""`. `node.get("date", {}).get("isoString", "")` then raises
    `'NoneType' object has no attribute 'get'`, and a whole CSV import lost
    that row to a "Metadata fetch failed" with nothing pointing at the cause.
    """
    value = _dig(node, "date").get("isoString")
    return value if isinstance(value, str) else ""


def _copyright_text(node: Any) -> str:
    """The joined copyright line of an album node. Null-safe, see _iso_date."""
    items = _dig(node, "copyright").get("items")
    if not isinstance(items, list):
        return ""
    return " \u00b7 ".join(
        c.get("text", "") for c in items if isinstance(c, dict) and c.get("text")
    )


def _is_explicit(node: Any) -> bool:
    """Whether a track node is marked explicit. Null-safe, see _iso_date."""
    return _dig(node, "contentRating").get("label") == "EXPLICIT"


def _name(node: Any, default: str) -> str:
    """A node's name, falling back when it is absent, null or blank.

    Blank counts: a restricted track carries `"name": ""`, and a track
    titled "" is a file named "".
    """
    if not isinstance(node, dict):
        return default
    value = node.get("name")
    return value if isinstance(value, str) and value.strip() else default


def _safe_playcount(raw: Any) -> str:
    """Reads the playcount from either a dict or a scalar value."""
    if isinstance(raw, dict):
        return str(raw.get("value") or "0")
    return str(raw or "0")


def _safe_duration_ms(raw: Any) -> int:
    """Reads the duration in ms from either a dict or a scalar value."""
    if isinstance(raw, dict):
        return int(raw.get("totalMilliseconds") or 0)
    return int(raw or 0)


def _extract_artist_names(artists_data: Any) -> list[str]:
    """Extracts artist names from the GraphQL structure or alternative lists."""
    if isinstance(artists_data, dict):
        items = artists_data.get("items", [])
        if isinstance(items, list) and items:
            names = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                profile = item.get("profile")
                if isinstance(profile, dict):
                    name = profile.get("name")
                else:
                    name = item.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
            return names

        profile = artists_data.get("profile")
        if isinstance(profile, dict):
            name = profile.get("name")
            return [name] if isinstance(name, str) and name else []

        name = artists_data.get("name")
        if isinstance(name, str) and name:
            return [name]

        return []

    if isinstance(artists_data, list):
        names = []
        for item in artists_data:
            if not isinstance(item, dict):
                continue
            profile = item.get("profile")
            if isinstance(profile, dict):
                name = profile.get("name")
            else:
                name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return names

    return []


def _join_artists(artists_data: Any) -> str:
    names = _extract_artist_names(artists_data)
    return ", ".join(names) if names else ""


def _best_cover(cover_urls: dict) -> str:
    return (
        cover_urls.get("large")
        or cover_urls.get("medium")
        or cover_urls.get("small", "")
    )


def _get_playlist_owner_data(playlist_v2: dict) -> dict:
    owner_data = playlist_v2.get("owner") or {}
    if not owner_data:
        owner_v2 = playlist_v2.get("ownerV2") or {}
        if isinstance(owner_v2, dict):
            owner_data = owner_v2.get("data") or {}
    if isinstance(owner_data, dict):
        return owner_data
    return {}


def _extract_playlist_owner(playlist_v2: dict) -> str:
    owner_data = _get_playlist_owner_data(playlist_v2)
    if not owner_data:
        return ""

    profile = owner_data.get("profile")
    if isinstance(profile, dict):
        return (
            profile.get("name", "")
            or owner_data.get("displayName", "")
            or owner_data.get("name", "")
        )

    return owner_data.get("displayName", "") or owner_data.get("name", "")


def _extract_playlist_owner_avatar(playlist_v2: dict) -> str:
    owner_data = _get_playlist_owner_data(playlist_v2)
    if not owner_data:
        return ""
    avatar_data = owner_data.get("avatar") or {}
    if not isinstance(avatar_data, dict):
        return ""
    return SpotifyWebClient().extract_cover_url(avatar_data)


def _extract_playlist_cover(playlist_v2: dict) -> str:
    images = playlist_v2.get("images") or {}
    if isinstance(images, dict):
        items = images.get("items") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                sources = item.get("sources") or []
                if isinstance(sources, list):
                    for source in sources:
                        if isinstance(source, dict):
                            url = source.get("url")
                            if isinstance(url, str) and url:
                                return url

        sources = images.get("sources") or []
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict):
                    url = source.get("url")
                    if isinstance(url, str) and url:
                        return url

    images_v2 = playlist_v2.get("imagesV2") or {}
    if isinstance(images_v2, dict):
        items = images_v2.get("items") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                sources = item.get("sources") or []
                if isinstance(sources, list):
                    for source in sources:
                        if isinstance(source, dict):
                            url = source.get("url")
                            if isinstance(url, str) and url:
                                return url

        sources = images_v2.get("sources") or []
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict):
                    url = source.get("url")
                    if isinstance(url, str) and url:
                        return url

    return ""


def _track_url(track_id: str) -> str:
    return f"https://open.spotify.com/track/{track_id}"


# ---------------------------------------------------------------------------
# URL parsing and utilities
# ---------------------------------------------------------------------------


#: A browser UA: the share-link interstitials serve a different page — and
#: sometimes no redirect at all — to anything that looks automated.
_SHORT_LINK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

#: Hosts and paths that carry no entity ID at all, only a token that has to
#: be exchanged for the real URL. These are what the mobile apps' share
#: sheets produce, so they are what users actually paste.
_SHORT_LINK_HOSTS = frozenset({"spotify.link", "spoti.fi"})

#: The canonical URL, as it appears in a short link's landing page.
_CANONICAL_PATTERNS = (
    re.compile(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)'),
    re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)'),
    re.compile(
        r"https://open\.spotify\.com/"
        r"(?:intl-[a-z-]+/)?"
        r"(?:track|album|playlist|artist)/[A-Za-z0-9]{22}",
    ),
)


def _is_short_link(u: Any) -> bool:
    if u.netloc in _SHORT_LINK_HOSTS:
        return True
    # open.spotify.com/s/<token> — same idea, Spotify's own host.
    return u.netloc in ("open.spotify.com", "play.spotify.com") and u.path.startswith(
        "/s/",
    )


def resolve_short_link(uri: str) -> str:
    """A share short link expanded to the URL it points at.

    A short link carries a token, not an ID, so there is nothing in it to
    parse — the only way to learn what it means is to ask. The redirect
    target is preferred; some of these land on an interstitial that carries
    the real URL only in its markup, so the body is scanned as well.

    Synchronous on purpose. parse_spotify_url() is called from both sync
    and async code and changing that would reach into every caller, so the
    one blocking request is confined here, given a short timeout, and only
    ever runs for the handful of URLs that cannot be parsed offline.
    """
    import httpx

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=8.0,
            headers={"User-Agent": _SHORT_LINK_UA},
        ) as client:
            resp = client.get(uri)
            final = str(resp.url)
            if not _is_short_link(urlparse(final)):
                return final
            for pattern in _CANONICAL_PATTERNS:
                match = pattern.search(resp.text)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
    except Exception as exc:
        logger.debug("[spotify] short link %s could not be resolved: %s", uri, exc)
    return ""


def parse_spotify_url(uri: str, _resolved: bool = False) -> dict[str, str]:
    u = urlparse(uri)

    # embed.spotify.com → redirect via the ?uri= query param
    if u.netloc == "embed.spotify.com":
        qs = parse_qs(u.query)
        if not qs.get("uri"):
            raise InvalidUrlError(uri)
        return parse_spotify_url(qs["uri"][0])

    if _is_short_link(u):
        # `_resolved` bounds this to one round trip: a short link that
        # redirects to another short link would otherwise recurse until the
        # stack ran out, and a chain that long is a redirect loop, not a
        # share URL.
        if _resolved:
            raise InvalidUrlError(uri)
        expanded = resolve_short_link(uri)
        if not expanded:
            raise InvalidUrlError(uri)
        return parse_spotify_url(expanded, _resolved=True)

    if u.scheme == "spotify":
        parts = uri.split(":")
    elif u.netloc in ("open.spotify.com", "play.spotify.com"):
        parts = u.path.split("/")
        if len(parts) > 1 and parts[1] == "embed":
            parts = parts[1:]
        if len(parts) > 1 and parts[1].startswith("intl-"):
            parts = parts[1:]
    elif not u.scheme and not u.netloc:
        path = u.path.strip()
        # ID bare da 22 caratteri → trattato come playlist
        if re.match(r"^[A-Za-z0-9]{22}$", path):
            return {"type": "playlist", "id": path}
        raise InvalidUrlError(uri)
    else:
        raise InvalidUrlError(uri)

    if len(parts) == 3 and parts[1] in ("album", "track", "playlist", "artist"):
        return {"type": parts[1], "id": parts[2].split("?")[0]}

    # Playlist nested (/user/<uid>/playlist/<id>)
    if len(parts) == 5 and parts[3] == "playlist":
        return {"type": "playlist", "id": parts[4].split("?")[0]}

    if len(parts) >= 4 and parts[1] == "artist":
        artist_id = parts[2].split("?")[0]
        if parts[3] == "discography":
            # Supporto sub-type: all / album / single / compilation / appears_on
            sub = parts[4].split("?")[0] if len(parts) >= 5 else "all"
            if sub not in _DISCOGRAPHY_SUBTYPES:
                sub = "all"
            return {"type": "artist_discography", "id": artist_id, "group": sub}
        return {"type": "artist", "id": artist_id}

    raise InvalidUrlError(uri)


def _normalize_artist(s: str) -> str:
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _artist_in_track(artist_name: str, track_artists: str) -> bool:
    name_norm = _normalize_artist(artist_name)
    return any(_normalize_artist(a) == name_norm for a in track_artists.split(","))


def _extract_discography_release(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    releases = item.get("releases")
    if isinstance(releases, dict):
        release_items = releases.get("items") or []
        if isinstance(release_items, list) and release_items:
            first = release_items[0]
            if isinstance(first, dict):
                return first
    album = item.get("album")
    if isinstance(album, dict):
        return album
    return {}


def _normalize_release_type(release_type: str) -> str:
    if not isinstance(release_type, str):
        return "single"
    normalized = release_type.upper()
    if normalized == "ALBUM":
        return "album"
    if normalized == "COMPILATION":
        return "compilation"
    if normalized == "APPEARS_ON":
        return "appears_on"
    return "single"


def _extract_release_id(release: dict[str, Any]) -> str:
    if not isinstance(release, dict):
        return ""
    release_id = release.get("id") or ""
    if release_id:
        return release_id
    uri = release.get("uri", "")
    if isinstance(uri, str) and ":" in uri:
        return uri.split(":")[-1]
    return ""


# ---------------------------------------------------------------------------
# Client GraphQL Unificato
# ---------------------------------------------------------------------------


class SpotifyMetadataClient:
    def __init__(self, timeout_s: int = 10) -> None:
        self.web_client = SpotifyWebClient()
        self.web_client.initialize()
        self._async_http = AsyncHttpClient(provider="spotify", timeout_s=timeout_s)

    def search(self, query: str, limit: int = 50) -> dict[str, list]:
        """Synchronous wrapper used by the GUI search backend."""
        return _run_async_sync(self.search_async, query, limit=limit)

    def get_url(
        self,
        url: str,
        include_featuring: bool = True,
    ) -> tuple[str, list[TrackMetadata], str, dict]:
        """Synchronous wrapper used by GUI metadata fetch and downloader."""
        return _run_async_sync(
            self.get_url_async,
            url,
            include_featuring=include_featuring,
        )

    # ------------------------------------------------------------------
    # Single track
    # ------------------------------------------------------------------

    async def _get_album_artists_async(self, album_id: str) -> list[str]:
        """Lightweight query: album metadata only, no track."""
        payload = {
            "operationName": "getAlbum",
            "variables": {
                "uri": f"spotify:album:{album_id}",
                "locale": "",
                "offset": 0,
                "limit": 1,
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10",
                },
            },
        }
        try:
            data = await asyncio.to_thread(self.web_client.query, payload)
            album_union = _dig(data, "data", "albumUnion")
            return _extract_artist_names(album_union.get("artists", {}))
        except Exception as e:
            logger.debug(f"[spotify] Failed to fetch album artists for {album_id}: {e}")
            return []

    async def _isrc_for_track_async(self, track_id: str) -> str:
        """The track's ISRC, or "" — never raises.

        An ISRC is worth having but never worth failing a metadata fetch
        over, so every failure here degrades to the old behaviour (no ISRC)
        rather than propagating.
        """
        try:
            return await asyncio.to_thread(
                self.web_client.get_isrc_from_metadata, track_id
            )
        except Exception as exc:
            logger.debug("[spotify] no ISRC for %s: %s", track_id, exc)
            return ""

    async def get_track_async(self, track_id: str) -> TrackMetadata:
        """Retrieves complete metadata for a single track, composer included."""
        payload = {
            "operationName": "getTrack",
            "variables": {"uri": f"spotify:track:{track_id}"},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294",
                },
            },
        }
        data = await asyncio.to_thread(self.web_client.query, payload)
        track_union = _dig(data, "data", "trackUnion")
        if not track_union:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                f"Spotify returned no metadata for track {track_id}.",
            )

        # A track this account cannot see — pulled from the catalogue, or
        # released only in other countries — answers with the shape intact
        # and every field emptied: no name, no duration, null `date`. That
        # null is what raised "'NoneType' object has no attribute 'get'"
        # halfway through a CSV import; parsing it instead would put an
        # untitled row in the track list that nothing can download. Saying
        # which track and why is more use than either.
        playability = _dig(track_union, "playability")
        if not _name(track_union, "") and playability.get("playable") is False:
            reason = str(playability.get("reason") or "").replace("_", " ").lower()
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"Track {track_id} is unavailable"
                + (f" ({reason})" if reason else "")
                + " — Spotify sent no metadata for it.",
            )

        album_data = _dig(track_union, "albumOfTrack")
        cover = self.web_client.extract_cover_url(_dig(album_data, "coverArt"))

        # albumOfTrack in getTrack non include artists → fetch separato
        album_artists_list = _extract_artist_names(album_data.get("artists"))
        if not album_artists_list:
            album_id = album_data.get("id") or ""
            if not album_id:
                uri = album_data.get("uri", "")
                if isinstance(uri, str) and ":" in uri:
                    album_id = uri.split(":")[-1]
            if album_id:
                album_artists_list = await self._get_album_artists_async(album_id)
            if not album_artists_list:
                album_artists_list = ["Unknown Artist"]
        album_artists_str = ", ".join(album_artists_list)

        # ------------------------------------------------------------------
        # Artist extraction logic:
        # ------------------------------------------------------------------
        artists_list = []

        # 1. Extract the first artist
        first = track_union.get("firstArtist")
        if first:
            artists_list.extend(_extract_artist_names(first))

        # 2. Extract the other artists
        others = track_union.get("otherArtists")
        if others:
            artists_list.extend(_extract_artist_names(others))

        # 3. Additional support for the standard artists structure
        if not artists_list:
            artists_list.extend(_extract_artist_names(track_union.get("artists")))

        # 4. Fallback to the album if necessary
        if not artists_list:
            artists_list = _extract_artist_names(album_data.get("artists"))

        # 5. Final fallback if everything fails
        if not artists_list:
            artists_list = ["Unknown Artist"]

        artists_str = ", ".join(artists_list)
        # ------------------------------------------------------------------

        # The ISRC comes from spclient's own metadata endpoint, which the
        # GraphQL query above does not carry. It used to be fetched only by
        # enrich_track_async() — a method nothing ever called, so every track
        # left this function with isrc="". That silently disabled a lot:
        # find_existing_track() falls back to title+artist matching,
        # _with_musicbrainz_tags() returns early, the local tagger's ISRC
        # identity check can never fire, and files SpotiFLAC writes carry no
        # ISRC for the next run to recognise.
        #
        # Gathered with the composer lookup rather than awaited after it, so
        # the extra request costs no wall-clock time.
        composer_str, isrc_str = await asyncio.gather(
            asyncio.to_thread(self.web_client.get_track_composer, track_id),
            self._isrc_for_track_async(track_id),
        )

        copyright_str = _copyright_text(album_data)

        return TrackMetadata(
            id=track_id,
            title=_name(track_union, "Unknown"),
            artists=artists_str,
            artist_names=artists_list,
            album=_name(album_data, "Unknown"),
            album_artist=album_artists_str,
            album_artist_names=album_artists_list,
            isrc=isrc_str,
            track_number=track_union.get("trackNumber") or 0,
            disc_number=track_union.get("discNumber") or 1,
            total_tracks=0,
            duration_ms=_safe_duration_ms(track_union.get("duration")),
            release_date=_iso_date(album_data),
            cover_url=cover,
            external_url=_track_url(track_id),
            copyright=copyright_str,
            composer=composer_str,
            preview_url="",
            plays=_safe_playcount(track_union.get("playcount")),
            is_explicit=_is_explicit(track_union),
        )

    # ------------------------------------------------------------------
    # Lazy Loading - Anteprima track
    # ------------------------------------------------------------------

    async def get_track_preview_async(self, track_id: str) -> str:
        """Retrieves the preview URL for a track at request time (lazy loading).

        This method is designed to be invoked only when the user clicks 'play' or 'preview'
        in the GUI, avoiding network requests during the initial list load.
        """
        try:
            preview_url = await asyncio.to_thread(
                self.web_client.get_preview_url,
                track_id,
            )
            return preview_url or ""
        except Exception as e:
            logger.debug(f"[spotify] Failed to fetch preview for track {track_id}: {e}")
            return ""

    # ------------------------------------------------------------------
    # Album
    # ------------------------------------------------------------------

    async def get_album_tracks_async(
        self,
        album_id: str,
    ) -> tuple[dict, list[TrackMetadata]]:
        """Retrieves all tracks of an album with complete pagination."""
        limit = 1000
        all_items: list[Any] = []
        album_union: dict = {}

        offset = 0
        while True:
            payload = {
                "operationName": "getAlbum",
                "variables": {
                    "uri": f"spotify:album:{album_id}",
                    "locale": "",
                    "offset": offset,
                    "limit": limit,
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10",
                    },
                },
            }
            data = await asyncio.to_thread(self.web_client.query, payload)
            au = _dig(data, "data", "albumUnion")

            # Save the album metadata only on the first pass
            if not album_union:
                album_union = au

            tracks_v2 = _dig(au, "tracksV2")
            items = tracks_v2.get("items") or []
            if not items:
                break

            all_items.extend(items)
            total_count = tracks_v2.get("totalCount", 0)
            if len(all_items) >= total_count or len(items) < limit:
                break
            offset += limit

        album_name = _name(album_union, "Unknown Album")
        cover = self.web_client.extract_cover_url(_dig(album_union, "coverArt"))
        album_artists_list = _extract_artist_names(album_union.get("artists"))
        album_artists = ", ".join(album_artists_list)
        release_date = _iso_date(album_union)
        total_tracks = _dig(album_union, "tracksV2").get("totalCount") or 0

        copyright_str = _copyright_text(album_union)

        tracks: list[TrackMetadata] = []
        for item in all_items:
            track_node = _dig(item, "track")

            track_id = track_node.get("id")
            if not track_id:
                uri = track_node.get("uri", "")
                if ":" in uri:
                    track_id = uri.split(":")[-1]

            if not track_id:
                continue

            track_artists_list = (
                _extract_artist_names(track_node.get("artists")) or album_artists_list
            )
            track_artists = ", ".join(track_artists_list) or album_artists

            tracks.append(
                TrackMetadata(
                    id=track_id,
                    title=_name(track_node, "Unknown"),
                    artists=track_artists,
                    artist_names=track_artists_list,
                    album=album_name,
                    album_artist=album_artists,
                    album_artist_names=album_artists_list,
                    isrc="",
                    track_number=track_node.get("trackNumber") or 0,
                    disc_number=track_node.get("discNumber") or 1,
                    total_tracks=total_tracks,
                    duration_ms=_safe_duration_ms(track_node.get("duration")),
                    release_date=release_date,
                    cover_url=cover,
                    external_url=_track_url(track_id),
                    copyright=copyright_str,
                    composer="",
                    preview_url="",
                    plays=_safe_playcount(track_node.get("playcount")),
                    is_explicit=_is_explicit(track_node),
                ),
            )

        return {
            "name": album_name,
            "cover_url": cover,
            "release_date": release_date,
        }, tracks

    # ------------------------------------------------------------------
    # Playlist
    # ------------------------------------------------------------------

    async def get_playlist_tracks_async(
        self,
        playlist_id: str,
    ) -> tuple[dict, list[TrackMetadata], str]:
        limit = 1000
        offset = 0
        all_items: list[Any] = []
        playlist_name = "Unknown Playlist"
        playlist_cover = playlist_owner = playlist_desc = ""
        followers = 0

        while True:
            payload = {
                "operationName": "fetchPlaylist",
                "variables": {
                    "uri": f"spotify:playlist:{playlist_id}",
                    "offset": offset,
                    "limit": limit,
                    "enableWatchFeedEntrypoint": False,
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77",
                    },
                },
            }
            response = await asyncio.to_thread(self.web_client.query, payload)
            playlist_v2 = _dig(response, "data", "playlistV2")

            if playlist_name == "Unknown Playlist" and playlist_v2:
                playlist_name = playlist_v2.get("name", "Unknown Playlist")
                playlist_desc = playlist_v2.get("description", "")
                f_raw = playlist_v2.get("followers")
                followers = (
                    f_raw.get("totalCount", 0)
                    if isinstance(f_raw, dict)
                    else int(f_raw or 0)
                )
                playlist_owner = _extract_playlist_owner(playlist_v2)
                playlist_cover = self.web_client.extract_cover_url(
                    playlist_v2.get("images", {}),
                ) or _extract_playlist_cover(playlist_v2)
                playlist_owner_avatar = _extract_playlist_owner_avatar(playlist_v2)

            content = _dig(playlist_v2, "content")
            items = content.get("items") or []
            if not items:
                break

            all_items.extend(items)
            if len(all_items) >= (content.get("totalCount") or 0) or len(items) < limit:
                break
            offset += limit

        tracks: list[TrackMetadata] = []
        for item in all_items:
            track_data = _dig(item, "itemV2", "data")
            track_id = track_data.get("id")
            if not track_id:
                uri = track_data.get("uri", "")
                if ":" in uri:
                    track_id = uri.split(":")[-1]
            if not track_id:
                continue

            album_data = _dig(track_data, "albumOfTrack")
            artists_list = _extract_artist_names(track_data.get("artists"))
            if not artists_list:
                artists_list = _extract_artist_names(album_data.get("artists")) or [
                    "Unknown Artist",
                ]

            cover = self.web_client.extract_cover_url(_dig(album_data, "coverArt"))
            album_artists_list = (
                _extract_artist_names(album_data.get("artists")) or artists_list[:1]
            )
            album_artists = ", ".join(album_artists_list)

            copyright_str = _copyright_text(album_data)

            tracks.append(
                TrackMetadata(
                    id=track_id,
                    title=_name(track_data, "Unknown"),
                    artists=", ".join(artists_list) if artists_list else "Unknown",
                    artist_names=artists_list,
                    album=_name(album_data, "Unknown"),
                    album_artist=album_artists,
                    album_artist_names=album_artists_list,
                    isrc="",
                    track_number=track_data.get("trackNumber") or 0,
                    disc_number=1,
                    total_tracks=0,
                    duration_ms=_safe_duration_ms(track_data.get("trackDuration")),
                    release_date="",
                    cover_url=cover,
                    external_url=_track_url(track_id),
                    copyright=copyright_str,
                    composer="",
                    preview_url="",
                    plays=_safe_playcount(track_data.get("playcount")),
                    is_explicit=_is_explicit(track_data),
                ),
            )

        info = {
            "name": playlist_name,
            "owner": playlist_owner,
            "owner_avatar": playlist_owner_avatar,
            "cover_url": playlist_cover,
            "description": playlist_desc,
            "followers": followers,
            "track_count": len(tracks),
            "source": "Spotify",
        }
        return info, tracks, playlist_cover

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    _SEARCH_HASH = "fcad5a3e0d5af727fb76966f06971c19cfa2275e6ff7671196753e008611873c"

    def _search_payload(self, query: str, limit: int, offset: int = 0) -> dict:
        return {
            "operationName": "searchDesktop",
            "variables": {
                "searchTerm": query,
                "offset": offset,
                "limit": limit,
                "numberOfTopResults": 5,
                "includeAudiobooks": True,
                "includeArtistHasConcertsField": False,
                "includePreReleases": True,
                "includeAuthors": False,
            },
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": self._SEARCH_HASH},
            },
        }

    async def search_async(self, query: str, limit: int = 20) -> dict[str, list]:
        """Unified search: returns tracks, albums, artists, and playlists."""
        try:
            data = await asyncio.to_thread(
                self.web_client.query,
                self._search_payload(query, limit),
            )
            search_v2 = _dig(data, "data", "searchV2")
        except Exception as e:
            logger.debug(f"[spotify] Search error: {e}")
            return {"tracks": [], "albums": [], "artists": [], "playlists": []}

        def _parse_tracks(items: list) -> list[TrackMetadata]:
            results = []
            for item in items:
                t = _dig(item, "item", "data")
                if not t.get("id"):
                    continue
                album_node = _dig(t, "albumOfTrack")
                track_artists_list = _extract_artist_names(t.get("artists"))
                track_artists_str = ", ".join(track_artists_list)
                album_artists_list = (
                    _extract_artist_names(album_node.get("artists"))
                    or track_artists_list
                )
                album_artists_str = ", ".join(album_artists_list)

                cover = self.web_client.extract_cover_url(_dig(album_node, "coverArt"))
                results.append(
                    TrackMetadata(
                        id=t["id"],
                        title=_name(t, "Unknown"),
                        artists=track_artists_str,
                        artist_names=track_artists_list,
                        album=_name(album_node, "Unknown"),
                        album_artist=album_artists_str,
                        album_artist_names=album_artists_list,
                        isrc="",
                        track_number=0,
                        disc_number=1,
                        total_tracks=0,
                        duration_ms=_safe_duration_ms(t.get("duration")),
                        release_date="",
                        cover_url=cover,
                        external_url=_track_url(t["id"]),
                        copyright="",
                        composer="",
                        preview_url="",
                        plays=_safe_playcount(t.get("playcount")),
                        is_explicit=_is_explicit(t),
                    ),
                )
            return results

        def _parse_simple(items: list, kind: str) -> list[dict]:
            results = []
            for item in items:
                node = item.get("data") or _dig(item, "item", "data")
                # La GraphQL di Spotify non espone sempre un campo `id` diretto su
                # album/artist/playlist: the ID is embedded in the URI
                # (es. "spotify:album:4aawyAB9vmqN3uQ7FjRGTy").
                # Try the direct field first, then extract it from the URI.
                node_id: str = node.get("id") or ""
                if not node_id:
                    uri = node.get("uri", "")
                    if isinstance(uri, str) and ":" in uri:
                        node_id = uri.split(":")[-1]
                if not node_id:
                    continue
                entry: dict[str, Any] = {
                    "id": node_id,
                    "type": kind,
                    "subtitle": kind.capitalize(),
                    "name": node.get("name") or _name(_dig(node, "profile"), "Unknown"),
                    "external_url": f"https://open.spotify.com/{kind}/{node_id}",
                }
                if kind == "album":
                    entry["artists"] = _join_artists(node.get("artists"))
                    entry["release_date"] = _iso_date(node)
                    entry["cover_url"] = self.web_client.extract_cover_url(
                        _dig(node, "coverArt"),
                    )
                elif kind == "artist":
                    cover_url = self.web_client.extract_cover_url(
                        _dig(node, "visualIdentity"),
                    )
                    if not cover_url:
                        alt_cover_data = _dig(node, "visuals", "avatarImage")
                        cover_url = self.web_client.extract_cover_url(alt_cover_data)
                    entry["cover_url"] = cover_url
                elif kind == "playlist":
                    owner = _dig(node, "owner")
                    if not owner:
                        owner = _dig(node, "ownerV2", "data")
                    entry["owner"] = owner.get("displayName") or owner.get("name") or ""
                    entry["cover_url"] = self.web_client.extract_cover_url(
                        _dig(node, "images"),
                    ) or _extract_playlist_cover(node)
                results.append(entry)
            return results

        tracks_data = search_v2.get("tracksV2") or search_v2.get("tracks") or {}
        albums_data = search_v2.get("albumsV2") or search_v2.get("albums") or {}
        artists_data = search_v2.get("artistsV2") or search_v2.get("artists") or {}
        playlists_data = (
            search_v2.get("playlistsV2") or search_v2.get("playlists") or {}
        )

        return {
            "tracks": _parse_tracks(tracks_data.get("items") or []),
            "albums": _parse_simple(albums_data.get("items") or [], "album"),
            "artists": _parse_simple(artists_data.get("items") or [], "artist"),
            "playlists": _parse_simple(playlists_data.get("items") or [], "playlist"),
        }

    async def search_by_type_async(
        self,
        query: str,
        kind: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list:
        """Filtered search for a single type: track | album | artist | playlist."""
        if kind not in ("track", "album", "artist", "playlist"):
            msg = f"Invalid type: {kind!r}. Valori ammessi: track, album, artist, playlist"
            raise ValueError(
                msg,
            )
        data = await asyncio.to_thread(
            self.web_client.query,
            self._search_payload(query, limit, offset),
        )
        data.get("data", {}).get("searchV2", {})
        results = await self.search_async(query, limit=limit)
        key = "tracks" if kind == "track" else f"{kind}s"
        return results.get(key, [])[offset:]

    async def search_tracks_async(
        self,
        query: str,
        limit: int = 20,
    ) -> list[TrackMetadata]:
        res = await self.search_async(query, limit=limit)
        return res["tracks"]

    # ------------------------------------------------------------------
    # Artist
    # ------------------------------------------------------------------

    async def get_artist_profile_async(self, artist_id: str) -> dict:
        payload = {
            "operationName": "queryArtistOverview",
            "variables": {"uri": f"spotify:artist:{artist_id}", "locale": ""},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "446130b4a0aa6522a686aafccddb0ae849165b5e0436fd802f96e0243617b5d8",
                },
            },
        }
        try:
            response = await asyncio.to_thread(self.web_client.query, payload)
            artist_data = response.get("data", {}).get("artistUnion", {})
            if not artist_data:
                return {}

            profile = artist_data.get("profile") or {}
            stats = artist_data.get("stats") or {}

            avatar_node = artist_data.get("visuals", {}).get("avatarImage", {})
            sources = avatar_node.get("sources", []) or (
                avatar_node.get("data") or {}
            ).get("sources", [])
            avatar_url = sources[0].get("url") if sources else None

            h_node = artist_data.get("headerImage", {})
            h_sources = h_node.get("sources", []) or (h_node.get("data") or {}).get(
                "sources",
                [],
            )
            header_url = h_sources[0].get("url") if h_sources else None

            return {
                "id": artist_id,
                "profile": {
                    "name": (profile or {}).get("name", ""),
                    "biography": re.sub(
                        r"<[^>]+>",
                        "",
                        (
                            ((profile.get("biography") or {}).get("text") or "")
                            if isinstance(profile.get("biography"), dict)
                            else (profile.get("biography") or "")
                        ),
                    ),
                    "verified": bool((profile or {}).get("verified", False)),
                },
                "stats": {
                    "followers": int(stats.get("followers") or 0),
                    "listeners": int(stats.get("monthlyListeners") or 0),
                    "rank": int(stats["worldRank"]) if stats.get("worldRank") else None,
                },
                "avatar": avatar_url,
                "header": header_url,
                "discography_total": int(
                    artist_data.get("discography", {}).get("all", {}).get("totalCount")
                    or 0,
                ),
            }
        except Exception as e:
            logger.warning(f"[spotify] Profile fetch failed: {e}")
            return {"profile": {"name": "Unknown"}, "stats": {}}

    async def get_artist_albums_async(
        self,
        artist_id: str,
        include_groups: str = "album,single",
        include_featuring: bool = True,
    ) -> tuple[dict, list[TrackMetadata]]:
        artist_info = await self.get_artist_profile_async(artist_id)

        items = await asyncio.to_thread(
            self.web_client.get_artist_discography,
            artist_id,
        )
        allowed_groups = (
            {"album", "single", "appears_on", "compilation"}
            if include_groups == "all"
            else set(include_groups.split(","))
        )
        if include_featuring and include_groups != "all":
            allowed_groups |= {"appears_on", "compilation"}

        albums_to_fetch: list[str] = []
        seen: set[str] = set()

        for item in items:
            release = _extract_discography_release(item)
            aid = _extract_release_id(release)
            if not aid or aid in seen:
                continue
            if _normalize_release_type(release.get("type", "")) not in allowed_groups:
                continue
            seen.add(aid)
            albums_to_fetch.append(aid)

        all_tracks: list[TrackMetadata] = []
        sem = asyncio.Semaphore(5)

        async def fetch_album(aid: str):
            async with sem:
                return await self.get_album_tracks_async(aid)

        tasks = [fetch_album(aid) for aid in albums_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                logger.warning("[spotify] Album fetch failed in discography: %s", res)
            else:
                _, track_list = res
                all_tracks.extend(track_list)

        return artist_info, all_tracks

    async def get_home_feed_async(self, time_zone: str = "Europe/Rome") -> dict:
        raw = await asyncio.to_thread(self.web_client.get_home_feed, time_zone)
        return parse_home_feed(raw)

    async def get_browse_categories_async(self) -> dict:
        raw = await asyncio.to_thread(self.web_client.get_browse_categories)
        return self._parse_browse(raw)

    def _parse_browse(self, raw: dict) -> dict:
        sections = (
            raw.get("data", {})
            .get("browseV2", {})
            .get("data", {})
            .get("sections", {})
            .get("items", [])
        )
        categories = []
        for section in sections:
            section_title = (section.get("data") or {}).get("title", {}).get("text", "")
            for item in section.get("sectionItems", {}).get("items", []):
                content = item.get("content", {}).get("data", {})
                card = (content.get("data") or {}).get("cardRepresentation", {})
                name = card.get("title", {}).get("text") or content.get("name", "")
                if not name:
                    continue
                uri = content.get("uri", "")
                cat_id = uri.split(":")[-1] if ":" in uri else ""
                categories.append(
                    {
                        "id": cat_id,
                        "uri": uri,
                        "name": name,
                        "image_url": (
                            (card.get("artwork") or {}).get("sources") or [{}]
                        )[0].get("url", ""),
                        "background_color": (card.get("backgroundColor") or {}).get(
                            "hex",
                            "",
                        ),
                        "section": section_title,
                    },
                )
        return {"success": True, "categories": categories}

    async def enrich_track_async(self, track: TrackMetadata) -> TrackMetadata:
        """Arricchisce ISRC, genre, label, copyright via Spotify metadata + Deezer."""
        if not track.id:
            return track

        # 1. ISRC da Spotify
        isrc = track.isrc or await asyncio.to_thread(
            self.web_client.get_isrc_from_metadata,
            track.id,
        )

        # 2. Deezer per label/copyright/genre
        if isrc:
            deezer = await self._fetch_deezer_by_isrc_async(isrc)
            return TrackMetadata(
                **{
                    **track.__dict__,
                    "isrc": isrc,
                    "label": deezer.get("label", ""),
                    "copyright": deezer.get("copyright", track.copyright),
                    "genre": deezer.get("genre", ""),
                },
            )
        return TrackMetadata(**{**track.__dict__, "isrc": isrc})

    async def _fetch_deezer_by_isrc_async(self, isrc: str) -> dict:
        try:
            resp = await self._async_http.get(
                f"https://api.deezer.com/track/isrc:{isrc}",
                timeout=8,
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
            if not data.get("id"):
                return {}
            result = {"isrc": data.get("isrc", isrc)}
            album_id = (data.get("album") or {}).get("id")
            if album_id:
                album_resp = await self._async_http.get(
                    f"https://api.deezer.com/album/{album_id}",
                    timeout=8,
                )
                if album_resp.status_code == 200:
                    album = album_resp.json()
                    if album.get("label"):
                        result["label"] = album["label"]
                        year = (album.get("release_date") or "")[:4]
                        result["copyright"] = f"{year} {album['label']}"
                    genres = [
                        g["name"] for g in (album.get("genres") or {}).get("data", [])
                    ]
                    if genres:
                        result["genre"] = ", ".join(genres)
            return result
        except Exception as e:
            logger.debug(f"[spotify] Deezer enrich failed for ISRC {isrc}: {e}")
            return {}

    # ------------------------------------------------------------------
    # Dispatcher principale
    # ------------------------------------------------------------------

    async def get_url_async(
        self,
        spotify_url: str,
        include_featuring: bool = True,
    ) -> tuple[str, list[TrackMetadata], str, dict]:
        info = parse_spotify_url(spotify_url)
        t = info["type"]
        logger.info(f"[DEBUG] URL type: {t}, ID: {info['id']}")

        routing_metadata = {
            "genre_source_preference": "musicbrainz" if t == "album" else "provider",
        }

        if t == "track":
            meta = await self.get_track_async(info["id"])
            return meta.title, [meta], "", routing_metadata

        if t == "album":
            album, tracks = await self.get_album_tracks_async(info["id"])
            album_meta = {
                "cover_url": album.get("cover_url", ""),
                "release_date": album.get("release_date", ""),
                "track_count": len(tracks),
            }
            album_meta.update(routing_metadata)
            return (
                album.get("name", "Unknown Album"),
                tracks,
                album.get("cover_url", ""),
                album_meta,
            )

        if t == "playlist":
            playlist, tracks, cover = await self.get_playlist_tracks_async(info["id"])
            playlist.update(routing_metadata)
            return playlist.get("name", "Unknown Playlist"), tracks, cover, playlist

        if t in ("artist", "artist_discography"):
            # Respect the discography sub-type if present in the URL
            group = info.get("group", "album,single")
            artist, tracks = await self.get_artist_albums_async(
                info["id"],
                include_groups=group,
                include_featuring=include_featuring,
            )
            artist_meta = {
                "name": artist.get("profile", {}).get("name", "Unknown Artist"),
                "profile": artist.get("profile", {}),
                "followers": artist.get("stats", {}).get("followers"),
                "listeners": artist.get("stats", {}).get("listeners"),
                "rank": artist.get("stats", {}).get("rank"),
                "avatar": artist.get("avatar"),
                "header": artist.get("header"),
                "verified": artist.get("profile", {}).get("verified", False),
                "biography": artist.get("profile", {}).get("biography", ""),
                "discography_total": artist.get("discography_total", 0),
            }
            artist_meta.update(routing_metadata)
            return (
                artist_meta["name"],
                tracks,
                artist_meta.get("avatar", ""),
                artist_meta,
            )

        raise SpotiflacError(ErrorKind.INVALID_URL, f"Spotify type not supported: {t}")

    async def get_metadata_from_url_async(self, url: str) -> TrackMetadata:
        _, tracks, _, _ = await self.get_url_async(url)
        if not tracks:
            msg = f"No tracks found for: {url}"
            raise ValueError(msg)
        return tracks[0]


def _extract_explore_artists(content: dict[str, Any]) -> str:
    artist_items = []
    raw_artists = content.get("artists")
    if isinstance(raw_artists, dict):
        artist_items = raw_artists.get("items") or []
    elif isinstance(raw_artists, list):
        artist_items = raw_artists

    names = []
    for artist in artist_items:
        if not isinstance(artist, dict):
            continue
        profile = artist.get("profile")
        name = profile.get("name") if isinstance(profile, dict) else artist.get("name")
        if isinstance(name, str) and name:
            names.append(name)

    if names:
        return ", ".join(names)

    artist_obj = content.get("artist") or {}
    if isinstance(artist_obj, dict):
        profile = artist_obj.get("profile")
        if isinstance(profile, dict):
            name = profile.get("name")
            if isinstance(name, str) and name:
                return name
        fallback_name = artist_obj.get("name")
        if isinstance(fallback_name, str) and fallback_name:
            return fallback_name

    subtitle = content.get("subtitle") or content.get("secondaryText") or ""
    if isinstance(subtitle, str) and subtitle:
        return subtitle.strip()

    return ""


def parse_home_feed(raw_data: dict) -> dict:
    """Formatta i dati grezzi dell'Home Feed per la GUI."""
    home_data = raw_data.get("data", {}).get("home", {})
    greeting = home_data.get("greeting", {}).get("text", "")

    sections_data = (
        home_data.get("sectionContainer", {}).get("sections", {}).get("items", [])
    )
    sections = []

    for sec_item in sections_data:
        sec_data = sec_item.get("data", {})
        title = sec_data.get("title", {}).get("text", "")
        if not title:
            continue

        sec_uri = sec_item.get("uri", "")

        sec_items = sec_item.get("sectionItems", {}).get("items", [])
        items = []

        for item in sec_items:
            content = item.get("content", {}).get("data", {})
            uri = content.get("uri", "")
            if not uri:
                continue

            parts = uri.split(":")
            if len(parts) < 3:
                continue

            item_type = parts[1]
            item_id = parts[2]

            name = content.get("name") or content.get("profile", {}).get("name", "")
            cover_url = ""
            artists = ""
            description = content.get("description", "")

            album_id = ""
            album_name = ""
            duration_ms = 0

            if item_type == "album":
                sources = content.get("coverArt", {}).get("sources", [])
                if sources:
                    cover_url = sources[0].get("url", "")
                artist_items = content.get("artists", {}).get("items", [])
                if artist_items:
                    artists = ", ".join(
                        a.get("profile", {}).get("name", "")
                        for a in artist_items
                        if a.get("profile", {}).get("name")
                    )

            elif item_type == "playlist":
                sources = (
                    content.get("images", {}).get("items", [{}])[0].get("sources", [])
                )
                if sources:
                    cover_url = sources[0].get("url", "")
                artists = content.get("ownerV2", {}).get("data", {}).get("name", "")

            elif item_type == "artist":
                sources = (
                    content.get("visuals", {}).get("avatarImage", {}).get("sources", [])
                )
                if sources:
                    cover_url = sources[0].get("url", "")

            elif item_type == "track":
                sources = (
                    content.get("albumOfTrack", {})
                    .get("coverArt", {})
                    .get("sources", [])
                )
                if sources:
                    cover_url = sources[0].get("url", "")
                artists = _extract_explore_artists(content)

                album_uri = content.get("albumOfTrack", {}).get("uri", "")
                album_id = album_uri.split(":")[-1] if ":" in album_uri else ""
                album_name = content.get("albumOfTrack", {}).get("name", "")
                dur = content.get("duration") or content.get("trackDuration") or {}
                duration_ms = (
                    dur.get("totalMilliseconds", 0) if isinstance(dur, dict) else 0
                )

            items.append(
                {
                    "id": item_id,
                    "uri": uri,
                    "type": item_type,
                    "name": name,
                    "artists": artists,
                    "description": description,
                    "cover_url": cover_url,
                    "album_id": album_id,
                    "album_name": album_name,
                    "duration_ms": duration_ms,
                    "provider_id": "spotify-web",
                },
            )
        if items:
            sections.append({"title": title, "uri": sec_uri, "items": items})

    return {"success": True, "greeting": greeting, "sections": sections}


def _maximize_cover_url(url: str) -> str:
    """Modifies the cover URL to request its highest-quality version."""
    if not url:
        return ""

    import re

    # Spotify encodes the size in the path prefix: 64px, 300px, 640px and
    # 1500px of the same artwork. 640 (…b273) is the one the API hands out
    # and was as far as this went; …82c1 is the same image at 1500x1500 and
    # is served for every cover the CDN has, so there is no reason to embed
    # the smaller one in a lossless file.
    for small in ("ab67616d00001e02", "ab67616d00004851", "ab67616d0000b273"):
        url = url.replace(small, "ab67616d000082c1")

    url = url.replace("ab67616100005174", "ab6761610000e5eb")
    url = url.replace("ab6761610000f178", "ab6761610000e5eb")

    if "mzstatic.com/image" in url:
        url = re.sub(r"/\d+x\d+([a-zA-Z]*)\.(jpg|webp|png)", r"/2000x2000\1.\2", url)

    # Tidal (Forza 1280x1280)
    if "resources.tidal.com/images" in url:
        url = re.sub(r"/\d+x\d+\.jpg", "/1280x1280.jpg", url)

    # Deezer (Forza 1000x1000)
    if "dzcdn.net/images" in url:
        url = re.sub(r"/\d+x\d+-", "/1000x1000-", url)

    # Qobuz (max original resolution)
    if "static.qobuz.com/images" in url:
        url = re.sub(r"_\d+\.jpg", "_max.jpg", url)

    # SoundCloud (Forza originale/500x500)
    if "sndcdn.com/artworks" in url:
        url = re.sub(r"-t\d+x\d+\.jpg", "-t500x500.jpg", url)

    return url

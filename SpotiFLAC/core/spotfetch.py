import base64
import json
import logging
import re
import threading
import time
from typing import Any

import httpx

# Uses the relative import path matching spotfetch.py's actual location
from SpotiFLAC.core.http import SAFE_ACCEPT_ENCODING
from SpotiFLAC.core.isrc_utils import is_valid_isrc
from SpotiFLAC.core.spotify_protobuf import (
    id_to_gid_hex,
    merge_fallbacks,
    parse_album,
    parse_track,
)
from SpotiFLAC.core.spotify_totp import generate_spotify_totp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Native metadata cache
# ---------------------------------------------------------------------------
#
# An album fetch is made once per *track* of that album otherwise: a 26-track
# release would ask spclient for the same barcode 26 times. Bounded in both
# directions — an LRU cap so a long library scan cannot grow it without limit,
# and a TTL because market-scoped fields (availability, the album a track
# resolves to) do change under us.

_NATIVE_CACHE_LIMIT = 500
_NATIVE_TTL_S = 300.0
#: Misses expire faster than hits: a release the endpoint did not know about
#: a moment ago may well be there on the next pass.
_NEGATIVE_TTL_S = 30.0

_native_track_cache: dict[str, tuple[dict[str, Any], float, float]] = {}
_native_album_cache: dict[str, tuple[dict[str, Any], float, float]] = {}
_native_cache_lock = threading.Lock()


def _native_cache_get(
    cache: dict[str, tuple[dict[str, Any], float, float]],
    key: str,
) -> dict[str, Any] | None:
    """The cached entry, or None when absent or stale.

    An empty dict is a *negative* entry and is returned as such — the caller
    must distinguish it from None, or a miss would be re-fetched anyway and
    the negative caching would buy nothing.
    """
    if not key:
        return None
    with _native_cache_lock:
        entry = cache.get(key)
        if entry is None:
            return None
        value, created_at, ttl_s = entry
        if time.monotonic() - created_at > ttl_s:
            cache.pop(key, None)
            return None
        # Refresh recency so the LRU eviction below drops genuinely cold keys.
        cache[key] = cache.pop(key)
        return value


def _native_cache_put(
    cache: dict[str, tuple[dict[str, Any], float, float]],
    key: str,
    value: dict[str, Any],
    ttl_s: float = _NATIVE_TTL_S,
) -> dict[str, Any]:
    if not key:
        return value
    with _native_cache_lock:
        cache.pop(key, None)
        cache[key] = (value, time.monotonic(), ttl_s)
        while len(cache) > _NATIVE_CACHE_LIMIT:
            cache.pop(next(iter(cache)))
    return value


def _isrc_from_raw_body(body: bytes) -> str:
    """Last-resort ISRC scrape, for a message the parser could not walk.

    This is the heuristic the parser replaced: find "isrc" followed by field
    control bytes and take the next 12-character alphanumeric block. It can
    match a neighbouring field, so the candidate is always validated against
    the real ISRC shape (2 letters + 3 alphanumeric + 7 digits) — a hit like
    "INTERNATIONA" is 12 letters with no digits and is never an ISRC.

    Kept only as a fallback: if Spotify renumbers the external-id field the
    structured read returns nothing and this still finds the code.
    """
    for match in re.finditer(rb"isrc[\x00-\x1f]+([A-Za-z0-9]{12})", body):
        candidate = match.group(1).decode(errors="ignore").upper()
        if is_valid_isrc(candidate):
            return candidate
    return ""


class SpotifyWebClient:
    """Client per interagire con le API interne (Web Player/GraphQL v2) di Spotify."""

    def __init__(self) -> None:
        # We use httpx.Client instead of requests.Session for instant connections
        limits = httpx.Limits(max_keepalive_connections=15, max_connections=30)
        self._session = httpx.Client(
            limits=limits,
            timeout=15.0,
            # See SAFE_ACCEPT_ENCODING: httpx 0.28.1 mis-decodes some
            # multi-frame zstd bodies, so zstd is not advertised.
            headers={"Accept-Encoding": SAFE_ACCEPT_ENCODING},
        )
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            },
        )
        self.access_token = ""
        self.client_token = ""
        self.client_id = ""
        self.device_id = ""
        self.client_version = ""

    def _get_session_info(self) -> None:
        """Retrieves la clientVersion e i cookie iniziali (sp_t)."""
        # Allineato a Go per retrievesre i parametri di sessione
        resp = self._session.get("https://open.spotify.com")
        resp.raise_for_status()

        match = re.search(
            r'<script[^>]+id=["\']appServerConfig["\'][^>]*>([^<]+)</script>',
            resp.text,
            re.IGNORECASE,
        )
        if match:
            try:
                decoded = base64.b64decode(match.group(1)).decode("utf-8")
                cfg = json.loads(decoded)
                self.client_version = cfg.get("clientVersion", self.client_version)
            except Exception as e:
                logger.debug(f"[spotfetch] Error decodifica appServerConfig: {e}")

        if not self.client_version:
            fallback = re.search(r'"clientVersion"\s*:\s*"([^"]+)"', resp.text)
            if fallback:
                self.client_version = fallback.group(1)
                logger.debug(
                    f"[spotfetch] clientVersion fallback extracted: {self.client_version}",
                )

        self.device_id = self._session.cookies.get("sp_t", "")
        if not self.device_id:
            cookie_header = resp.headers.get("set-cookie", "")
            cookie_match = re.search(r"sp_t=([^;]+)", cookie_header)
            if cookie_match:
                self.device_id = cookie_match.group(1)
        logger.debug(f"[spotfetch] _get_session_info: device_id={self.device_id}")

    def _get_access_token(self) -> None:
        """Generates the TOTP and obtains the first access token (endpoint: /api/token)."""
        code, ver = generate_spotify_totp()

        params = {
            "reason": "init",
            "productType": "web-player",
            "totp": code,
            "totpVer": str(ver),
            "totpServer": code,
        }

        # Headers matching the Go code
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Content-Type": "application/json;charset=UTF-8",
        }

        try:
            resp = self._session.get(
                "https://open.spotify.com/api/token",
                params=params,
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()

            data = resp.json()
            self.access_token = data.get("accessToken", "")
            self.client_id = data.get("clientId", "")
            logger.debug(
                f"[spotfetch] Access token acquired: {self.access_token[:20] if self.access_token else 'empty'}...",
            )

            # Extract sp_t cookie
            if not self.device_id:
                self.device_id = self._session.cookies.get("sp_t", "")

        except Exception as e:
            logger.exception(f"[spotfetch] Failed to get access token: {e}")
            raise

    def _get_client_token(self) -> None:
        """Performs device binding and obtains the final Client-Token."""
        if not (self.client_id and self.device_id and self.client_version):
            self._get_session_info()
            self._get_access_token()

        payload = {
            "client_data": {
                "client_version": self.client_version,
                "client_id": self.client_id,
                "js_sdk_data": {
                    "device_brand": "unknown",
                    "device_model": "unknown",
                    "os": "windows",
                    "os_version": "NT 10.0",
                    "device_id": self.device_id,
                    "device_type": "computer",
                },
            },
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        resp = self._session.post(
            "https://clienttoken.spotify.com/v1/clienttoken",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()

        data = resp.json()
        if data.get("response_type") == "RESPONSE_GRANTED_TOKEN_RESPONSE":
            self.client_token = data.get("granted_token", {}).get("token", "")
        else:
            logger.error(f"[spotfetch] Unexpected clienttoken response: {data}")
            msg = "Spotify client token request did not return a granted token"
            raise RuntimeError(
                msg,
            )

    def initialize(self, force: bool = False) -> None:
        if force:
            self.access_token = ""
            self.client_token = ""
            self.device_id = ""
            # Recreate session to clear cookies
            limits = httpx.Limits(max_keepalive_connections=15, max_connections=30)
            self._session = httpx.Client(
                limits=limits,
                timeout=15.0,
                # See SAFE_ACCEPT_ENCODING: httpx 0.28.1 mis-decodes some
                # multi-frame zstd bodies, so zstd is not advertised.
                headers={"Accept-Encoding": SAFE_ACCEPT_ENCODING},
            )
            self._session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
                },
            )

        if force or not self.client_version or not self.device_id:
            self._get_session_info()

        if not self.access_token:
            self._get_access_token()

        if not self.client_token:
            self._get_client_token()

    def extract_cover_image(self, cover_data: dict) -> dict:
        """Advanced cover resolution algorithm; extracts the highest resolution using hash."""
        if not cover_data:
            return {}

        sources = cover_data.get("sources", [])
        if not sources:
            square = (
                cover_data.get("squareCoverImage", {}).get("image", {}).get("data", {})
            )
            sources = square.get("sources", [])

        if not sources:
            return {}

        filtered = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            url = s.get("url", "")
            if not url:
                continue
            width = s.get("width") or s.get("maxWidth") or 0
            height = s.get("height") or s.get("maxHeight") or 0

            if (width > 64 and height > 64) or (width == 0 and height == 0 and url):
                filtered.append({"url": url, "width": width, "height": height})

        filtered.sort(key=lambda x: x["width"])

        small_url, medium_url, fallback_url, image_id = "", "", "", ""
        for s in filtered:
            w, url = s["width"], s["url"]
            if w == 300:
                small_url = url
            elif w == 640:
                medium_url = url
            elif w == 0:
                fallback_url = url

            if not image_id and url:
                for prefix in [
                    "ab67616d0000b273",
                    "ab67616d00001e02",
                    "ab67616d00004851",
                ]:
                    if prefix in url:
                        image_id = url.split(prefix)[-1].split("?")[0].strip("/")
                        break
                if not image_id and "/image/" in url:
                    part = url.split("/image/")[-1].split("?")[0]
                    if len(part) > 20:
                        image_id = part

        large_url = (
            f"https://i.scdn.co/image/ab67616d000082c1{image_id}" if image_id else ""
        )

        res = {}
        if small_url:
            res["small"] = small_url
        if medium_url:
            res["medium"] = medium_url
        if large_url:
            res["large"] = large_url
        if not res and fallback_url:
            res = {"small": fallback_url, "medium": fallback_url, "large": fallback_url}

        return res

    def extract_cover_url(self, cover_data: dict) -> str:
        """Extracts the preferred cover URL without building a complete map."""
        if not cover_data or not isinstance(cover_data, dict):
            return ""

        direct_url = (
            cover_data.get("url") or cover_data.get("src") or cover_data.get("href")
        )
        if isinstance(direct_url, str) and direct_url:
            return direct_url

        sources = cover_data.get("sources")
        if sources is None:
            square = (
                cover_data.get("squareCoverImage", {}).get("image", {}).get("data", {})
            )
            if isinstance(square, dict):
                sources = square.get("sources")

        if isinstance(sources, list):
            preferred = ""
            fallback = ""
            for source in sources:
                if not isinstance(source, dict):
                    continue
                url = source.get("url")
                if not isinstance(url, str) or not url:
                    continue

                width = source.get("width") or source.get("maxWidth") or 0
                height = source.get("height") or source.get("maxHeight") or 0

                if width in {640, 300}:
                    return url
                if width >= 300 and height >= 300 and not preferred:
                    preferred = url
                if not fallback:
                    fallback = url

            return preferred or fallback or ""

        return ""

    def get_home_feed(self, time_zone: str = "Europe/Rome") -> dict:
        """Retrieves l'Home Feed di Spotify (Daily Mix, Nuove uscite, ecc.)."""
        payload = {
            "operationName": "home",
            "variables": {"timeZone": time_zone},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "3a67ee0ea6abad2ebad2e588a9aa130fc98d6b553f5b05ac6467503d02133bdc",
                },
            },
        }
        return self.query(payload)

    def get_browse_categories(self) -> dict:
        """Retrieves le categorie e i generi esplorabili."""
        payload = {
            "operationName": "browseAll",
            "variables": {},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "864fdecccb9bb893141df3776d0207886c7fa781d9e586b9d4eb3afa387eea42",
                },
            },
        }
        return self.query(payload)

    def get_track_composer(self, track_id: str) -> str:
        """Native GraphQL query to get composers without HTML scraping."""
        payload = {
            "variables": {
                "trackUri": f"spotify:track:{track_id}",
                "contributorsLimit": 100,
                "contributorsOffset": 0,
            },
            "operationName": "queryTrackCreditsModal",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "e2ca40d46cf1fde36562261ccec754f23fb31b561877252e9fe0d6834aabb84b",
                },
            },
        }
        try:
            data = self.query(payload)
            items = (
                data.get("data", {})
                .get("trackUnion", {})
                .get("creditsTrait", {})
                .get("contributors", {})
                .get("items", [])
            )
            composers = []
            for item in items:
                if item.get("role", "").strip().lower() == "composer":
                    name = item.get("name", "").strip()
                    if name and name not in composers:
                        composers.append(name)
            return ", ".join(composers)
        except Exception as exc:
            logger.debug(
                f"[spotfetch] Error recupero compositori per {track_id}: {exc}",
            )
            return ""

    def get_preview_url(self, track_id: str) -> str:
        """Retrieves the preview URL from the embed page (same logic as Go GetPreviewURL)."""
        try:
            embed_url = f"https://open.spotify.com/embed/track/{track_id}"
            resp = self._session.get(embed_url, timeout=10)
            if resp.status_code != 200:
                return ""
            match = re.search(
                r"https://p\.scdn\.co/mp3-preview/[a-zA-Z0-9]+",
                resp.text,
            )
            return match.group(0) if match else ""
        except Exception as exc:
            logger.debug(f"[spotfetch] Preview URL fetch failed for {track_id}: {exc}")
            return ""

    def query(self, payload: dict[str, Any], retry: bool = True) -> dict[str, Any]:
        """Esegue una query GraphQL autorizzata puntando all'endpoint pathfinder/v2/query."""
        if not (self.access_token and self.client_token):
            self.initialize()

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Token": self.client_token,
            "Spotify-App-Version": self.client_version,
            "Content-Type": "application/json",
        }
        logger.debug(
            f"[spotfetch] Sending GraphQL query: {payload.get('operationName', 'unknown')}",
        )
        # Allineato a Go: endpoint query V2
        resp = self._session.post(
            "https://api-partner.spotify.com/pathfinder/v2/query",
            json=payload,
            headers=headers,
        )
        logger.debug(f"[spotfetch] Response status: {resp.status_code}")

        if resp.status_code == 401 and retry:
            logger.debug("[spotfetch] Token scaduto. Auto-rinnovo in corso...")
            self.initialize(force=True)
            return self.query(payload, retry=False)

        if resp.status_code != 200:
            logger.error(
                f"[spotfetch] GraphQL query failed: HTTP {resp.status_code} | {resp.text[:500]}",
            )
            # Some responses (e.g. 412 Invalid query hash) carry a JSON body
            # that callers can interpret to fall back; we don't immediately
            # turn everything into an exception, to keep the higher-level
            # fallback logic simple.
            if resp.status_code == 412:
                try:
                    return resp.json()
                except Exception:
                    return {"error": resp.text}
            resp.raise_for_status()

        result = resp.json()
        logger.debug(f"[spotfetch] Response keys: {list(result.keys())}")
        return result

    def get_track_stats(self, track_id: str) -> dict:
        """Retrieves the playcount of a single track via Spotify's internal GraphQL API."""
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

        try:
            data = self.query(payload)
            logger.debug(
                f"[spotfetch] Full response for track {track_id}: {json.dumps(data)[:500]}",
            )

            # Direct extraction, Go-style
            # `data` is present-but-null on a partial GraphQL failure, so
            # the two-step .get() chain would raise AttributeError here.
            envelope = data.get("data") if isinstance(data, dict) else None
            track_data = (envelope or {}).get("trackUnion") or {}
            if not isinstance(track_data, dict):
                track_data = {}
            playcount = track_data.get("playcount", "")

            result = {
                "playcount": str(playcount) if playcount else "",
                "rank": "",
                "status": "",
            }
            logger.debug(f"[spotfetch] get_track_stats({track_id}) result: {result}")
            return result
        except Exception as exc:
            logger.debug(f"[spotfetch] Error retrieving track stats {track_id}: {exc}")
            return {"playcount": "", "rank": "", "status": ""}

    def get_playlist_stats(
        self,
        playlist_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        """Retrieves playcount, rank e status per le tracks all'interno di una playlist.
        Returns un dizionario con track_id come chiave.
        """
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

        stats_map = {}
        try:
            data = self.query(payload)

            # Extract items from the playlist
            items = (
                data.get("data", {})
                .get("playlistV2", {})
                .get("content", {})
                .get("items", [])
            )
            logger.debug(f"[spotfetch] Found {len(items)} items in playlist")

            for idx, item in enumerate(items):
                try:
                    track_data = item.get("itemV2", {}).get("data", {})

                    track_uri = track_data.get("uri", "")
                    track_id = track_data.get("id", "")
                    if not track_id and ":" in track_uri:
                        track_id = track_uri.split(":")[-1]

                    if not track_id:
                        continue

                    # Estrai playcount
                    playcount = track_data.get("playcount", "")

                    rank = ""
                    status = ""

                    for attr in item.get("attributes", []):
                        if isinstance(attr, dict):
                            key = attr.get("key")
                            if key == "rank":
                                rank = str(attr.get("value", ""))
                            elif key == "status":
                                status = str(attr.get("value", ""))

                    stats_map[track_id] = {
                        "playcount": str(playcount) if playcount else "",
                        "rank": rank,
                        "status": status,
                    }
                except Exception as item_err:
                    logger.debug(f"[spotfetch] Error processing item {idx}: {item_err}")
                    continue

            logger.debug(
                f"[spotfetch] Successfully extracted {len(stats_map)} tracks with stats",
            )
            return stats_map

        except Exception as exc:
            logger.debug(
                f"[spotfetch] Error recupero stats playlist {playlist_id}: {exc}",
            )
            return {}

    def get_album_stats(self, album_id: str, offset: int = 0, limit: int = 100) -> dict:
        """Retrieves the playcount of every track in an album in a single GraphQL request.
        Returns a dict keyed by track_id.
        """
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

        stats_map = {}
        try:
            data = self.query(payload)

            # Estrai items dall'album
            album_union = data.get("data", {}).get("albumUnion", {})
            tracks_v2 = album_union.get("tracksV2", {})
            items = tracks_v2.get("items", [])

            for idx, item in enumerate(items):
                try:
                    track = item.get("track", {})
                    if not track:
                        continue

                    track_uri = track.get("uri", "")
                    track_id = track.get("id", "")
                    if not track_id and ":" in track_uri:
                        track_id = track_uri.split(":")[-1]

                    if not track_id:
                        continue

                    # Estrai playcount
                    playcount = track.get("playcount", "")

                    stats_map[track_id] = {
                        "playcount": str(playcount) if playcount else "",
                        "rank": "",
                        "status": "",
                    }
                except Exception as item_err:
                    logger.debug(
                        f"[spotfetch] Error processing album item {idx}: {item_err}",
                    )
                    continue

            return stats_map

        except Exception as exc:
            logger.debug(f"[spotfetch] Error recupero stats album {album_id}: {exc}")
            return {}

    def get_artist_discography(
        self,
        artist_id: str,
        order: str = "DATE_DESC",
    ) -> list[dict[str, Any]]:
        """Retrieves the list of releases in an artist's discography via GraphQL.
        Returns the elements of `data.artistUnion.discography.all.items`.
        """
        all_items: list[dict[str, Any]] = []
        offset = 0
        limit = 50

        while True:
            payload = {
                "operationName": "queryArtistDiscographyAll",
                "variables": {
                    "uri": f"spotify:artist:{artist_id}",
                    "offset": offset,
                    "limit": limit,
                    "order": order,
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599",
                    },
                },
            }

            try:
                data = self.query(payload)
            except Exception as exc:
                logger.debug(
                    f"[spotfetch] Error recupero discografia artista {artist_id}: {exc}",
                )
                break

            discography = (
                data.get("data", {}).get("artistUnion", {}).get("discography", {})
            )
            all_data = discography.get("all", {})
            items = all_data.get("items", [])
            if not items:
                break

            all_items.extend(item for item in items if isinstance(item, dict))

            total_count = all_data.get("totalCount", 0) or 0
            try:
                total_count = int(total_count)
            except Exception:
                total_count = len(all_items)

            if len(all_items) >= total_count or len(items) < limit:
                break

            offset += limit

        return all_items

    def spotify_id_to_hex_gid(self, spotify_id: str) -> str:
        """Converte un Spotify base62 ID nel GID esadecimale richiesto dall'endpoint metadata."""
        return id_to_gid_hex(spotify_id)

    def _metadata_body(
        self,
        entity_type: str,
        spotify_id: str,
        _retried: bool = False,
    ) -> bytes | None:
        """The raw protobuf from spclient's binary metadata endpoint.

        `_retried` bounds the 401 path to a single refresh. A token that comes
        back from initialize() still unauthorised — a market the endpoint
        refuses, a revoked client token — used to recurse forever, each turn
        costing a full session bootstrap. This is awaited inside
        _isrc_for_track_async(), which is gathered into every single-track
        metadata load, so that recursion did not fail: it hung the load.
        """
        try:
            gid = id_to_gid_hex(spotify_id)
            resp = self._session.get(
                f"https://spclient.wg.spotify.com/metadata/4/{entity_type}/{gid}?market=from_token",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Client-Token": self.client_token,
                    "Spotify-App-Version": self.client_version,
                    "App-Platform": "WebPlayer",
                },
            )
            if resp.status_code == 401:
                if _retried:
                    logger.debug(
                        "[spotfetch] metadata endpoint still 401 after a token "
                        "refresh for %s %s — giving up",
                        entity_type,
                        spotify_id,
                    )
                    return None
                # force=True, as query() does on its own 401. Plain
                # initialize() only fills in credentials that are *missing*,
                # and a 401 here means the ones we hold are present and
                # expired — so the retry re-sent the same dead token and got
                # the same 401. The lookup never recovered; it just cost a
                # second round-trip before giving up.
                self.initialize(force=True)
                return self._metadata_body(entity_type, spotify_id, _retried=True)
            if resp.status_code != 200:
                return None
            return resp.content
        except Exception as e:
            logger.debug(
                "[spotfetch] metadata lookup failed for %s %s: %s",
                entity_type,
                spotify_id,
                e,
            )
            return None

    def get_native_album_metadata(self, album_id: str) -> dict[str, Any]:
        """The album's native metadata: UPC, disc layout, label, copyright.

        These have no other source in this codebase. The GraphQL album query
        carries neither the barcode nor the disc list, so DISCTOTAL was
        always written as 1 and UPC was never written at all.
        """
        if not album_id:
            return {}
        cached = _native_cache_get(_native_album_cache, album_id)
        if cached is not None:
            return cached

        body = self._metadata_body("album", album_id)
        if not body:
            # Negative entry, on a short TTL: a release the endpoint has no
            # answer for should not be re-asked once per track of it.
            return _native_cache_put(_native_album_cache, album_id, {}, _NEGATIVE_TTL_S)

        metadata = parse_album(body)
        metadata.setdefault("album_id", album_id)
        if not metadata.get("album_url"):
            metadata["album_url"] = f"https://open.spotify.com/album/{album_id}"
        return _native_cache_put(_native_album_cache, album_id, metadata)

    def get_native_track_metadata(
        self,
        track_id: str,
        album_id: str = "",
    ) -> dict[str, Any]:
        """The track's native metadata, completed from its album.

        The album embedded in a track response is a *subset* — it has the
        name, label and release date but no UPC, no disc list and no
        copyright. Those only exist in a separate album fetch, so one is
        made and merged in behind whatever the track already said.
        """
        if not track_id:
            return {}
        cached = _native_cache_get(_native_track_cache, track_id)
        if cached is not None:
            return cached

        body = self._metadata_body("track", track_id)
        if not body:
            return _native_cache_put(_native_track_cache, track_id, {}, _NEGATIVE_TTL_S)

        metadata = parse_track(body)
        if not metadata.get("isrc"):
            metadata["isrc"] = _isrc_from_raw_body(body)

        resolved_album = album_id or metadata.get("album_id") or ""
        if resolved_album:
            metadata.setdefault("album_id", resolved_album)
            album_metadata = self.get_native_album_metadata(resolved_album)
            if album_metadata:
                merge_fallbacks(metadata, album_metadata)

        return _native_cache_put(_native_track_cache, track_id, metadata)

    def get_isrc_from_metadata(self, track_id: str, _retried: bool = False) -> str:
        """The track's ISRC, or "".

        `_retried` is accepted for compatibility with the callers that used
        to drive the retry themselves; the bounded refresh now lives in
        _metadata_body().
        """
        try:
            return self.get_native_track_metadata(track_id).get("isrc", "")
        except Exception as e:
            logger.debug(f"[spotfetch] ISRC lookup failed for {track_id}: {e}")
            return ""

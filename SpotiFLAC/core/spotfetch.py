import base64
import json
import logging
import re
from typing import Any

import httpx

# Utilizza il path relativo corretto in base a dove hai saved spotfetch.py
from ..core.spotify_totp import generate_spotify_totp

logger = logging.getLogger(__name__)


class SpotifyWebClient:
    """Client per interagire con le API interne (Web Player/GraphQL v2) di Spotify."""

    def __init__(self) -> None:
        # Usiamo httpx.Client al posto di requests.Session per connessioni istantanee
        limits = httpx.Limits(max_keepalive_connections=15, max_connections=30)
        self._session = httpx.Client(limits=limits, timeout=15.0)
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            }
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
            re.I,
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
                    f"[spotfetch] clientVersion fallback extracted: {self.client_version}"
                )

        self.device_id = self._session.cookies.get("sp_t", "")
        if not self.device_id:
            cookie_header = resp.headers.get("set-cookie", "")
            cookie_match = re.search(r"sp_t=([^;]+)", cookie_header)
            if cookie_match:
                self.device_id = cookie_match.group(1)
        logger.debug(f"[spotfetch] _get_session_info: device_id={self.device_id}")

    def _get_access_token(self) -> None:
        """Genera il TOTP e ottiene il primo access token (endpoint: /api/token)."""
        code, ver = generate_spotify_totp()

        params = {
            "reason": "init",
            "productType": "web-player",
            "totp": code,
            "totpVer": str(ver),
            "totpServer": code,
        }

        # Headers come nel codice Go
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
                f"[spotfetch] Access token acquired: {self.access_token[:20] if self.access_token else 'empty'}..."
            )

            # Extract sp_t cookie
            if not self.device_id:
                self.device_id = self._session.cookies.get("sp_t", "")

        except Exception as e:
            logger.error(f"[spotfetch] Failed to get access token: {e}")
            raise

    def _get_client_token(self) -> None:
        """Esegue il binding del dispositivo e ottiene il Client-Token definitivo."""
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
            }
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
            raise RuntimeError(
                "Spotify client token request did not return a granted token"
            )

    def initialize(self) -> None:
        if not self.client_version or not self.device_id:
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

                if width == 640 or width == 300:
                    return url
                if width >= 300 and height >= 300 and not preferred:
                    preferred = url
                if not fallback:
                    fallback = url

            return preferred or fallback or ""

        return ""

    def get_home_feed(self, time_zone: str = "Europe/Rome") -> dict:
        """Retrieves l'Home Feed di Spotify (Daily Mix, Nuove uscite, ecc.)"""
        payload = {
            "operationName": "home",
            "variables": {"timeZone": time_zone},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "3a67ee0ea6abad2ebad2e588a9aa130fc98d6b553f5b05ac6467503d02133bdc",
                }
            },
        }
        return self.query(payload)

    def get_browse_categories(self) -> dict:
        """Retrieves le categorie e i generi esplorabili"""
        payload = {
            "operationName": "browseAll",
            "variables": {},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "864fdecccb9bb893141df3776d0207886c7fa781d9e586b9d4eb3afa387eea42",
                }
            },
        }
        return self.query(payload)

    def get_track_composer(self, track_id: str) -> str:
        """Query nativa GraphQL per ottenere i compositori senza scraping HTML."""
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
                }
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
                f"[spotfetch] Error recupero compositori per {track_id}: {exc}"
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
                r"https://p\.scdn\.co/mp3-preview/[a-zA-Z0-9]+", resp.text
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
            f"[spotfetch] Sending GraphQL query: {payload.get('operationName', 'unknown')}"
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
            self.initialize()
            return self.query(payload, retry=False)

        if resp.status_code != 200:
            logger.error(
                f"[spotfetch] GraphQL query failed: HTTP {resp.status_code} | {resp.text[:500]}"
            )
            # Alcune risposte (es. 412 Invalid query hash) contengono un body JSON
            # che i chiamanti possono interpretare per fare un fallback; non
            # trasformiamo immediatamente tutto in un'eccezione per semplificare
            # la logica di fallback a livello superiore.
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
        """
        Retrieves il playcount di una singola track tramite API GraphQL interna Spotify.
        """
        payload = {
            "operationName": "getTrack",
            "variables": {"uri": f"spotify:track:{track_id}"},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294",
                }
            },
        }

        try:
            data = self.query(payload)
            logger.debug(
                f"[spotfetch] Full response for track {track_id}: {json.dumps(data)[:500]}"
            )

            # Estrazione diretta in stile Go
            track_data = data.get("data", {}).get("trackUnion", {})
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
        self, playlist_id: str, offset: int = 0, limit: int = 100
    ) -> dict:
        """
        Retrieves playcount, rank e status per le tracks all'interno di una playlist.
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
                }
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
                f"[spotfetch] Successfully extracted {len(stats_map)} tracks with stats"
            )
            return stats_map

        except Exception as exc:
            logger.debug(
                f"[spotfetch] Error recupero stats playlist {playlist_id}: {exc}"
            )
            return {}

    def get_album_stats(self, album_id: str, offset: int = 0, limit: int = 100) -> dict:
        """
        Retrieves il playcount di tutte le tracks di un album in un'unica richiesta GraphQL.
        Returns un dizionario con track_id come chiave.
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
                }
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
                        f"[spotfetch] Error processing album item {idx}: {item_err}"
                    )
                    continue

            return stats_map

        except Exception as exc:
            logger.debug(f"[spotfetch] Error recupero stats album {album_id}: {exc}")
            return {}

    def get_artist_discography(
        self, artist_id: str, order: str = "DATE_DESC"
    ) -> list[dict[str, Any]]:
        """
        Retrieves the list of releases in an artist's discography via GraphQL.
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
                    }
                },
            }

            try:
                data = self.query(payload)
            except Exception as exc:
                logger.debug(
                    f"[spotfetch] Error recupero discografia artista {artist_id}: {exc}"
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
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        bytes_ = []
        for char in spotify_id:
            value = alphabet.index(char)
            carry = value
            for j in range(len(bytes_)):
                total = bytes_[j] * 62 + carry
                bytes_[j] = total & 0xFF
                carry = total >> 8
            while carry > 0:
                bytes_.append(carry & 0xFF)
                carry >>= 8
        while len(bytes_) < 16:
            bytes_.append(0)
        return "".join(f"{b:02x}" for b in reversed(bytes_))

    def get_isrc_from_metadata(self, track_id: str) -> str:
        """Retrieves l'ISRC dall'endpoint binario spclient (stesso approccio del JS)."""
        try:
            gid = self.spotify_id_to_hex_gid(track_id)
            resp = self._session.get(
                f"https://spclient.wg.spotify.com/metadata/4/track/{gid}?market=from_token",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Client-Token": self.client_token,
                    "Spotify-App-Version": self.client_version,
                    "App-Platform": "WebPlayer",
                },
            )
            if resp.status_code == 401:
                self.initialize()
                return self.get_isrc_from_metadata(track_id)
            if resp.status_code != 200:
                return ""
            import re

            match = re.search(rb"isrc[\x00-\x1f]+([A-Za-z0-9]{12})", resp.content)
            return match.group(1).decode().upper() if match else ""
        except Exception as e:
            logger.debug(f"[spotfetch] ISRC lookup failed for {track_id}: {e}")
            return ""

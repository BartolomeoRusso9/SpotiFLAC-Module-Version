import base64
import json
import logging
import re
from typing import Any

import requests

# Utilizza il path relativo corretto in base a dove hai salvato spotfetch.py
from ..core.spotify_totp import generate_spotify_totp

logger = logging.getLogger(__name__)

class SpotifyWebClient:
    """Client per interagire con le API interne (Web Player/GraphQL v2) di Spotify."""
    
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
        self.access_token = ""
        self.client_token = ""
        self.client_id = ""
        self.device_id = ""
        self.client_version = ""

    def _get_session_info(self) -> None:
        """Recupera la clientVersion e i cookie iniziali (sp_t)."""
        # Allineato a Go per recuperare i parametri di sessione
        resp = self._session.get("https://open.spotify.com")
        resp.raise_for_status()
        
        match = re.search(r'<script id="appServerConfig" type="text/plain">([^<]+)</script>', resp.text)
        if match:
            try:
                decoded = base64.b64decode(match.group(1)).decode('utf-8')
                cfg = json.loads(decoded)
                self.client_version = cfg.get("clientVersion", "")
            except Exception as e:
                logger.debug(f"[spotfetch] Errore decodifica appServerConfig: {e}")

        self.device_id = self._session.cookies.get("sp_t", "")

    def _get_access_token(self) -> None:
        """Genera il TOTP e ottiene il primo access token (endpoint: /api/token)."""
        code, ver = generate_spotify_totp()
        
        params = {
            "reason": "init",
            "productType": "web-player",
            "totp": code,
            "totpVer": str(ver),
            "totpServer": code
        }
        
        # Headers come nel codice Go
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Content-Type": "application/json;charset=UTF-8",
        }
        
        try:
            logger.debug(f"[spotfetch] Requesting access token from https://open.spotify.com/api/token")
            resp = self._session.get("https://open.spotify.com/api/token", params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            
            data = resp.json()
            self.access_token = data.get("accessToken", "")
            self.client_id = data.get("clientId", "")
            logger.debug(f"[spotfetch] Access token acquired: {self.access_token[:20] if self.access_token else 'empty'}...")
            
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
                    "device_type": "computer"
                }
            }
        }
        
        headers = {
            "Authority": "clienttoken.spotify.com",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        resp = self._session.post("https://clienttoken.spotify.com/v1/clienttoken", json=payload, headers=headers)
        resp.raise_for_status()
        
        data = resp.json()
        if data.get("response_type") == "RESPONSE_GRANTED_TOKEN_RESPONSE":
            self.client_token = data.get("granted_token", {}).get("token", "")

    def initialize(self) -> None:
        """Esegue l'intera pipeline di handshake."""
        self._get_session_info()
        self._get_access_token()
        self._get_client_token()

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Esegue una query GraphQL autorizzata puntando all'endpoint pathfinder/v2/query."""
        if not (self.access_token and self.client_token):
            self.initialize()
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Token": self.client_token,
            "Spotify-App-Version": self.client_version,
            "Content-Type": "application/json",
        }
        
        logger.debug(f"[spotfetch] Sending GraphQL query: {payload.get('operationName', 'unknown')}")
        # Allineato a Go: endpoint query V2
        resp = self._session.post("https://api-partner.spotify.com/pathfinder/v2/query", json=payload, headers=headers)
        logger.debug(f"[spotfetch] Response status: {resp.status_code}")
        
        if resp.status_code != 200:
            logger.error(f"[spotfetch] GraphQL query failed: HTTP {resp.status_code} | {resp.text[:500]}")
            resp.raise_for_status()
        
        result = resp.json()
        logger.debug(f"[spotfetch] Response data: {result}")
        return result
    
    def get_track_stats(self, track_id: str) -> dict:
        """
        Recupera il playcount di una singola traccia tramite API GraphQL interna Spotify.
        """
        payload = {
            "operationName": "getTrack",
            "variables": {
                "uri": f"spotify:track:{track_id}"
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294"
                }
            }
        }
        
        try:
            data = self.query(payload)
            logger.debug(f"[spotfetch] Full response for track {track_id}: {json.dumps(data)[:500]}")
            
            # Estrazione diretta in stile Go
            track_data = data.get("data", {}).get("trackUnion", {})
            playcount = track_data.get("playcount", "")
            
            result = {
                "playcount": str(playcount) if playcount else "",
                "rank": "",
                "status": ""
            }
            logger.debug(f"[spotfetch] get_track_stats({track_id}) result: {result}")
            return result
        except Exception as exc:
            logger.debug(f"[spotfetch] Errore recupero stats traccia {track_id}: {exc}")
            return {"playcount": "", "rank": "", "status": ""}

    def get_playlist_stats(self, playlist_id: str, offset: int = 0, limit: int = 100) -> dict:
        """
        Recupera playcount, rank e status per le tracce all'interno di una playlist.
        Restituisce un dizionario con track_id come chiave.
        """
        payload = {
            "operationName": "fetchPlaylist",
            "variables": {
                "uri": f"spotify:playlist:{playlist_id}",
                "offset": offset,
                "limit": limit,
                "enableWatchFeedEntrypoint": False
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77"
                }
            }
        }
        
        stats_map = {}
        try:
            data = self.query(payload)
            
            # Estrai items dalla playlist
            items = data.get("data", {}).get("playlistV2", {}).get("content", {}).get("items", [])
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
                        "status": status
                    }
                except Exception as item_err:
                    logger.debug(f"[spotfetch] Error processing item {idx}: {item_err}")
                    continue
            
            logger.debug(f"[spotfetch] Successfully extracted {len(stats_map)} tracks with stats")
            return stats_map
            
        except Exception as exc:
            logger.debug(f"[spotfetch] Errore recupero stats playlist {playlist_id}: {exc}")
            return {}

    def get_artist_discography(self, artist_id: str, order: str = "DATE_DESC") -> list[dict[str, Any]]:
        """
        Recupera la lista di release della discografia di un artista tramite GraphQL.
        Restituisce gli elementi di `data.artistUnion.discography.all.items`.
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
                        "sha256Hash": "5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599"
                    }
                }
            }

            try:
                data = self.query(payload)
            except Exception as exc:
                logger.debug(f"[spotfetch] Errore recupero discografia artista {artist_id}: {exc}")
                break

            discography = data.get("data", {}).get("artistUnion", {}).get("discography", {})
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
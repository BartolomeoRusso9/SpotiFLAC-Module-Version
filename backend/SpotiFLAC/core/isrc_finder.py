# backend/SpotiFLAC/core/isrc_finder.py

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_SPOTIFY_TRACK_ID_RE = re.compile(r"^(?:spotify:track:|https?://(?:open\.spotify\.com|play\.spotify\.com)/track/)?([A-Za-z0-9]{22})(?:[/?].*)?$")


def spotify_id_to_gid(track_id: str) -> str:
    if not track_id or not isinstance(track_id, str):
        raise ValueError("Invalid Spotify track identifier")

    match = _SPOTIFY_TRACK_ID_RE.match(track_id.strip())
    if not match:
        raise ValueError(f"Invalid Spotify track identifier: {track_id}")

    return match.group(1)


class IsrcFinder:
    def __init__(self, http_client):
        self.http = http_client
        self._spotify_client = None

    def _get_spotify_client(self):
        if self._spotify_client is None:
            try:
                from .spotfetch import SpotifyWebClient
                self._spotify_client = SpotifyWebClient()
                self._spotify_client.initialize()
            except Exception as e:
                logger.debug("[isrc_finder] Could not init SpotifyWebClient: %s", e)
        return self._spotify_client

    def find_isrc(self, track_id: str) -> Optional[str]:
        try:
            gid = spotify_id_to_gid(track_id)
        except ValueError as e:
            logger.debug("[isrc_finder] %s", e)
            return None

        client = self._get_spotify_client()
        if not client or not client.access_token:
            return None

        url = f"https://spclient.wg.spotify.com/metadata/4/track/{gid}"
        try:
            from .http import NetworkManager
            resp = NetworkManager.get_sync_client().get(
                url,
                headers={
                    "Authorization": f"Bearer {client.access_token}",
                    "Client-Token":   client.client_token,
                },
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                ids = data.get("external_id") or [{}]
                return ids[0].get("value")
            elif resp.status_code == 401:
                self._spotify_client = None
        except Exception as e:
            logger.debug("[isrc_finder] Mirror lookup failed: %s", e)
        return None
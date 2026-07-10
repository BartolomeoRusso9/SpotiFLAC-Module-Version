# backend/core/isrc_finder.py

import asyncio
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SPOTIFY_TRACK_ID_RE = re.compile(
    r"^(?:spotify:track:|https?://(?:open\.spotify\.com|play\.spotify\.com)(?:/intl-[^/]+)?/track/)?([A-Za-z0-9]{22})(?:[/?].*)?$"
)


def spotify_id_to_gid(track_id: str) -> str:
    if not track_id or not isinstance(track_id, str):
        raise ValueError("Invalid Spotify track identifier")

    match = _SPOTIFY_TRACK_ID_RE.match(track_id.strip())
    if not match:
        raise ValueError(f"Invalid Spotify track identifier: {track_id}")

    return match.group(1)


def _normalize_isrc(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    from .isrc_utils import is_valid_isrc, normalize_isrc

    isrc = normalize_isrc(value)
    return isrc if is_valid_isrc(isrc) else None


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

    async def find_isrc_async(self, track_id: str) -> Optional[str]:
        # NOTE: l'endpoint spclient.wg.spotify.com/metadata/4/track/{gid}
        # restituisce un blob protobuf binario (content-type
        # "vnd.spotify/metadata-track"), mai JSON. Un precedente tentativo
        # qui chiamava resp.json() su quel blob, fallendo sistematicamente
        # con errori di decodifica UTF-8 per ogni traccia. L'estrazione
        # (con validazione del formato ISRC reale) è centralizzata in
        # SpotifyWebClient.get_isrc_from_metadata, che va invocata invece
        # di duplicare qui un parsing JSON strutturalmente impossibile.
        try:
            spotify_id_to_gid(track_id)
        except ValueError as exc:
            logger.debug("[isrc_finder] %s", exc)
            return None

        client = self._get_spotify_client()
        if not client or not client.access_token or not client.client_token:
            logger.debug(
                "[isrc_finder] SpotifyWebClient is not initialized or missing access token"
            )
            return None

        try:
            isrc = await asyncio.to_thread(client.get_isrc_from_metadata, track_id)
        except Exception as exc:
            logger.debug("[isrc_finder] Spotify metadata lookup failed: %s", exc)
            return None

        return _normalize_isrc(isrc) if isrc else None
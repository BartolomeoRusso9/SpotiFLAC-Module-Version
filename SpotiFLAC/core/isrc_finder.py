# backend/core/isrc_finder.py

import asyncio
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SPOTIFY_TRACK_ID_RE = re.compile(
    r"^(?:spotify:track:|https?://(?:open\.spotify\.com|play\.spotify\.com)/track/)?([A-Za-z0-9]{22})(?:[/?].*)?$"
)
_SPOTIFY_METADATA_URL = "https://spclient.wg.spotify.com/metadata/4/track/{}"
_SPOTIFY_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_METADATA_ATTEMPTS = 3
_ISRC_RE = re.compile(r"^[A-Z0-9]{12}$")


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
    isrc = value.strip().upper()
    if _ISRC_RE.match(isrc):
        return isrc
    return None


def _extract_isrc_from_payload(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None

    external_ids = data.get("external_ids")
    if isinstance(external_ids, dict):
        isrc = _normalize_isrc(external_ids.get("isrc"))
        if isrc:
            return isrc

    ids_list = data.get("external_id")
    if isinstance(ids_list, list):
        for ext in ids_list:
            if not isinstance(ext, dict):
                continue
            if ext.get("type", "").strip().lower() == "isrc":
                isrc = _normalize_isrc(ext.get("id") or ext.get("value"))
                if isrc:
                    return isrc

        if ids_list and isinstance(ids_list[0], dict):
            return _normalize_isrc(ids_list[0].get("id") or ids_list[0].get("value"))

    return None


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

    async def _fetch_spotify_track_metadata(self, url: str, headers: dict[str, str]) -> Optional[dict[str, Any]]:
        for attempt in range(1, _MAX_METADATA_ATTEMPTS + 1):
            client = self._get_spotify_client()
            if not client or not client.access_token:
                logger.debug("[isrc_finder] SpotifyWebClient is not initialized or missing access token")
                return None

            try:
                from .http import NetworkManager

                async_client = await NetworkManager.get_async_client_safe()
                resp = await async_client.get(url, headers=headers, timeout=8.0)
            except Exception as exc:
                logger.debug(
                    "[isrc_finder] Spotify metadata request failed (attempt %s/%s): %s",
                    attempt,
                    _MAX_METADATA_ATTEMPTS,
                    exc,
                )
                if attempt == _MAX_METADATA_ATTEMPTS:
                    return None
                await asyncio.sleep(0.25 * attempt)
                continue

            if resp.status_code == 401:
                logger.debug("[isrc_finder] Spotify metadata auth failure, refreshing Spotify client")
                self._spotify_client = None
                if attempt == _MAX_METADATA_ATTEMPTS:
                    return None
                await asyncio.sleep(0.25 * attempt)
                continue

            if resp.status_code in _SPOTIFY_RETRY_STATUS:
                logger.debug(
                    "[isrc_finder] Spotify metadata transient failure %s, attempt %s/%s",
                    resp.status_code,
                    attempt,
                    _MAX_METADATA_ATTEMPTS,
                )
                if attempt == _MAX_METADATA_ATTEMPTS:
                    return None
                await asyncio.sleep(0.25 * attempt)
                continue

            if resp.status_code != 200:
                logger.debug(
                    "[isrc_finder] Spotify metadata request failed with status %s",
                    resp.status_code,
                )
                return None

            try:
                return resp.json()
            except Exception as exc:
                logger.debug("[isrc_finder] Failed to decode Spotify metadata JSON: %s", exc)
                return None

        return None

    async def find_isrc_async(self, track_id: str) -> Optional[str]:
        try:
            gid = spotify_id_to_gid(track_id)
        except ValueError as exc:
            logger.debug("[isrc_finder] %s", exc)
            return None

        try:
            client = self._get_spotify_client()
            if not client or not client.access_token or not client.client_token:
                return None

            gid = client.spotify_id_to_hex_gid(track_id)
        except Exception as exc:
            logger.debug("[isrc_finder] Invalid Spotify ID for hex conversion: %s", exc)
            return None

        payload = await self._fetch_spotify_track_metadata(
            _SPOTIFY_METADATA_URL.format(gid),
            {
                "Authorization": f"Bearer {client.access_token}",
                "Client-Token": client.client_token,
            },
        )

        if not payload:
            return None

        return _extract_isrc_from_payload(payload)

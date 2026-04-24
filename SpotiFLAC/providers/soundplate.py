import logging
from typing import Optional
from ..core.http import HttpClient

logger = logging.getLogger(__name__)

class SoundplateProvider:
    """Risolve ISRC tramite l'API di Soundplate."""

    API_URL = "https://isrc.soundplate.com/api/v1/isrc-search/spotify/"

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def get_isrc(self, track_id: str) -> Optional[str]:
        try:
            data = self.http.get_json(f"{self.API_URL}{track_id}")
            isrc = data.get("isrc")
            return isrc.upper() if isrc else None
        except Exception as e:
            logger.debug("[soundplate] Failed: %s", e)
            return None
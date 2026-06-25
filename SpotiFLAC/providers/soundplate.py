import logging
import re
from typing import Optional

from ..core.http import AsyncHttpClient

logger = logging.getLogger(__name__)

class SoundplateProvider:
    """Risolve ISRC tramite l'endpoint HTML di Soundplate."""

    API_URL = "https://soundplate.com/isrc-finder?track="

    def __init__(self, http_client: AsyncHttpClient):
        self.http = http_client

    async def get_isrc_async(self, track_id: str) -> Optional[str]:
        try:
            url = f"{self.API_URL}{track_id}"
            resp = await self.http.get(url, follow_redirects=True)
            match = re.search(r'(?i)\bISRC\b[^A-Z0-9]*([A-Z]{2}[A-Z0-9]{10})', resp.text)
            if match:
                return match.group(1).upper()
            return None
        except Exception as e:
            logger.debug("[soundplate] Failed: %s", e)
            return None

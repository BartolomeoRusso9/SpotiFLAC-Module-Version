import logging
from typing import Dict, Optional
from .http import HttpClient

logger = logging.getLogger(__name__)

class LinkResolver:
    """Risolve link tra piattaforme usando Odesli (Songlink)."""

    API_URL = "https://api.song.link/v1-alpha.1/links"

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def resolve_all(self, spotify_id: str) -> Dict[str, str]:
        """Ritorna un dizionario con i link per ogni piattaforma."""
        params = {
            "id": spotify_id,
            "platform": "spotify",
            "userCountry": "US"
        }
        links = {}
        try:
            data = self.http.get_json(self.API_URL, params=params)
            entities = data.get("linksByPlatform", {})
            for platform, info in entities.items():
                links[platform] = info.get("url")
        except Exception as e:
            logger.debug("[link_resolver] Odesli failed: %s", e)
        return links
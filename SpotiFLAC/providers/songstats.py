import re
import logging
from typing import Optional
from ..core.http import HttpClient

logger = logging.getLogger(__name__)

class SongstatsProvider:
    """Estrae ISRC dalla pagina pubblica di Songstats."""

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def get_isrc(self, track_id: str) -> Optional[str]:
        url = f"https://songstats.com/track/{track_id}"
        try:
            resp = self.http.get(url)
            # Regex basata sulla logica Go
            match = re.search(r'isrc\\":\\"(.*?)\\"', resp.text)
            if match:
                return match.group(1).upper()
        except Exception as e:
            logger.debug("[songstats] Failed: %s", e)
        return None
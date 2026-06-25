import json
import logging
import re
from typing import Dict, Optional

from ..core.http import AsyncHttpClient

logger = logging.getLogger(__name__)


class SongstatsProvider:
    """Extracts ISRC and platform links from the public Songstats page via JSON-LD."""

    def __init__(self, http_client: AsyncHttpClient):
        self.http = http_client

    async def get_isrc_async(self, track_id: str) -> Optional[str]:
        data = await self.get_data_async(track_id)
        return data.get("isrc")

    async def get_data_async(self, track_id: str) -> Dict[str, Optional[str]]:
        url = f"https://songstats.com/track/{track_id}"
        results = {"isrc": None, "tidal": None, "amazon": None, "deezer": None}

        try:
            resp = await self.http.get(url, follow_redirects=True)

            # 1. Fallback rapido per l'ISRC
            isrc_match = re.search(r'isrc\\":\\"(.*?)\\"', resp.text)
            if isrc_match:
                results["isrc"] = isrc_match.group(1).upper()

            # 2. Parsing strutturato JSON-LD per i link (Allineamento al Go)
            script_matches = re.finditer(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                resp.text,
                re.DOTALL | re.IGNORECASE,
            )

            for match in script_matches:
                try:
                    payload = json.loads(match.group(1).strip())
                    self._collect_links(payload, results)
                except json.JSONDecodeError:
                    continue

        except Exception as e:
            logger.debug("[songstats] Failed: %s", e)

        return results

    def _collect_links(self, data, results):
        if isinstance(data, dict):
            if "sameAs" in data:
                self._apply_same_as(data["sameAs"], results)
            for val in data.values():
                self._collect_links(val, results)
        elif isinstance(data, list):
            for item in data:
                self._collect_links(item, results)

    def _apply_same_as(self, same_as, results):
        if isinstance(same_as, str):
            self._assign_link(same_as, results)
        elif isinstance(same_as, list):
            for item in same_as:
                if isinstance(item, str):
                    self._assign_link(item, results)

    def _assign_link(self, link: str, results: Dict[str, Optional[str]]):
        link = link.strip()
        if not link:
            return

        if "listen.tidal.com/track" in link and not results["tidal"]:
            results["tidal"] = link
            logger.debug("✓ Tidal URL found via Songstats")
        elif "music.amazon.com" in link and not results["amazon"]:
            results["amazon"] = link
            logger.debug("✓ Amazon URL found via Songstats")
        elif "deezer.com" in link and not results["deezer"]:
            results["deezer"] = link
            logger.debug("✓ Deezer URL found via Songstats")

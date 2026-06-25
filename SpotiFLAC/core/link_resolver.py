import json
import logging
import urllib.parse
import re
from typing import Dict, Optional

from .http import AsyncHttpClient, async_songlink_rate_limiter

logger = logging.getLogger(__name__)

class LinkResolver:
    """Resolves cross-platform links using a Multi-Provider approach (Async-only)."""

    SONGLINK_API_URL = "https://api.song.link/v1-alpha.1/links"
    DEEZER_ISRC_API = "https://api.deezer.com/track/isrc:{}"
    DEEZER_TRACK_API = "https://api.deezer.com/track/{}"

    _SONGLINK_PLATFORMS = (
        "deezer", "amazonMusic", "tidal", "appleMusic", "spotify", "soundcloud"
    )

    def __init__(self, http_client: AsyncHttpClient | None = None):
        self.http = http_client or AsyncHttpClient("songlink", rate_limiter=async_songlink_rate_limiter)
        self._deezer_async_cache = {}

    async def _safe_get_json(self, url: str, params: Optional[dict] = None) -> dict:
        """Helper robusto che aggiunge User-Agent e Accept per evitare il blocco 406 di Varnish/WAF."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        if hasattr(self.http, "get_json_async"):
            try:
                return await self.http.get_json_async(url, params=params, headers=headers)
            except TypeError:
                return await self.http.get_json_async(url, params=params)
        
        # Fallback per httpx.AsyncClient nativo con spoofing dei metadati di navigazione
        resp = await self.http.get(url, params=params, headers=headers)
        return resp.json()

    async def _safe_get_html(self, url: str):
        """Helper robusto che emula un browser desktop per le richieste di scraping HTML."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/ *;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1"
        }
        if hasattr(self.http, "get_async"):
            try:
                return await self.http.get_async(url, headers=headers)
            except TypeError:
                return await self.http.get_async(url)
        
        # Fallback per httpx.AsyncClient nativo
        return await self.http.get(url, headers=headers)

    def identify_provider(self, url: str) -> str:
        url = url.lower()
        if "soundcloud.com" in url or "on.soundcloud.com" in url:
            return "soundcloud"
        elif "spotify.com" in url:
            return "spotify"
        return "unknown"
    
    def _normalize_amazon_url(self, raw_url: str) -> str:
        url = raw_url.strip()
        if not url:
            return ""
        if "trackAsin=" in url:
            parts = url.split("trackAsin=")
            if len(parts) > 1:
                track_asin = parts[1].split("&")[0]
                if track_asin:
                    return f"https://music.amazon.com/tracks/{track_asin}?musicTerritory=US"

        amazon_album_track = re.search(r'/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})', url, re.IGNORECASE)
        if amazon_album_track:
            return f"https://music.amazon.com/tracks/{amazon_album_track.group(1)}?musicTerritory=US"

        amazon_track = re.search(r'/tracks/(B[0-9A-Z]{9})', url, re.IGNORECASE)
        if amazon_track:
            return f"https://music.amazon.com/tracks/{amazon_track.group(1)}?musicTerritory=US"

        return url

    def _extract_deezer_id(self, raw_url: str) -> str:
        clean_url = raw_url.strip()
        if not clean_url:
            return ""
        parts = clean_url.split("/track/")
        if len(parts) < 2:
            return ""
        return parts[1].split("?")[0].strip("/ ")

    def _normalize_deezer_url(self, raw_url: str) -> str:
        track_id = self._extract_deezer_id(raw_url)
        if track_id:
            return f"https://www.deezer.com/track/{track_id}"
        return raw_url.strip()

    def _process_songlink_response(self, data: dict) -> Dict[str, str]:
        links: dict[str, str] = {}
        entities = data.get("linksByPlatform", {})

        for platform in self._SONGLINK_PLATFORMS:
            entry = entities.get(platform)
            if isinstance(entry, dict):
                url = entry.get("url")
                if url:
                    links[platform] = self._normalize_platform_url(platform, url)

        return links

    def _normalize_platform_url(self, platform: str, url: str) -> str:
        url = url.strip()
        if not url:
            return ""
        if platform == "deezer":
            return self._normalize_deezer_url(url)
        if platform == "amazonMusic":
            return self._normalize_amazon_url(url)
        return url

    def _merge_links(self, final_links: dict[str, str], new_links: dict[str, str]) -> None:
        for platform, url in new_links.items():
            if platform not in final_links and url:
                final_links[platform] = url

    def _process_songstats_links(self, html: str) -> Dict[str, str]:
        links = {"amazonMusic": "", "tidal": "", "deezer": ""}
        matches = re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
        for match in matches:
            try:
                payload = json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue
            self._collect_songstats_links(payload, links)
        return {k: v for k, v in links.items() if v}

    def _collect_songstats_links(self, data, results: dict[str, str]) -> None:
        if isinstance(data, dict):
            same_as = data.get("sameAs")
            if isinstance(same_as, list):
                for url in same_as:
                    if isinstance(url, str):
                        self._assign_songstats_link(url, results)
            for val in data.values():
                self._collect_songstats_links(val, results)
        elif isinstance(data, list):
            for item in data:
                self._collect_songstats_links(item, results)

    def _assign_songstats_link(self, link: str, results: dict[str, str]) -> None:
        link = link.strip()
        if not link:
            return
        if "listen.tidal.com/track" in link and not results.get("tidal"):
            results["tidal"] = link
        elif "music.amazon.com" in link and not results.get("amazonMusic"):
            results["amazonMusic"] = self._normalize_amazon_url(link)
        elif "deezer.com" in link and not results.get("deezer"):
            results["deezer"] = self._normalize_deezer_url(link)

    async def _get_isrc_from_deezer_async(self, deezer_url: str) -> str:
        track_id = self._extract_deezer_id(deezer_url)
        if not track_id:
            return ""
        try:
            url = self.DEEZER_TRACK_API.format(track_id)
            data = await self._safe_get_json(url)
            isrc = data.get("isrc", "")
            if isrc:
                return isrc.upper().strip()
        except Exception as e:
            logger.debug(f"[link_resolver] Error during ISRC reverse lookup on Deezer async: {e}")
        return ""

    async def _get_deezer_url_by_isrc_async(self, isrc: str) -> str:
        isrc_clean = isrc.upper().strip()
        if isrc_clean in self._deezer_async_cache:
            return self._deezer_async_cache[isrc_clean]
            
        try:
            url = self.DEEZER_ISRC_API.format(isrc_clean)
            data = await self._safe_get_json(url)
            
            res = ""
            if "link" in data and data["link"]:
                res = self._normalize_deezer_url(data["link"])
            elif "id" in data and data["id"] > 0:
                res = f"https://www.deezer.com/track/{data['id']}"
                
            self._deezer_async_cache[isrc_clean] = res
            return res
        except Exception as e:
            logger.debug(f"[link_resolver] Deezer ISRC lookup async failed: {e}")
        return ""

    async def _get_songlink_links_async(self, params: dict[str, str]) -> dict[str, str]:
        try:
            data = await self._safe_get_json(self.SONGLINK_API_URL, params=params)
            return self._process_songlink_response(data)
        except Exception as e:
            logger.debug(f"[link_resolver] Songlink lookup async failed: {e}")
        return {}

    async def _get_songlink_links_by_url_async(self, url: str) -> dict[str, str]:
        return await self._get_songlink_links_async({"url": url, "userCountry": "US"})

    async def _get_songlink_links_by_id_async(self, raw_id: str, platform: str) -> dict[str, str]:
        return await self._get_songlink_links_async({"id": raw_id, "platform": platform, "userCountry": "US"})

    async def _get_songlink_html_links_async(self, raw_id: str) -> Dict[str, str]:
        links: dict[str, str] = {}
        try:
            url = f"https://song.link/s/{urllib.parse.quote(raw_id, safe='')}?userCountry=US"
            resp = await self._safe_get_html(url)
            html = resp.text

            deezer_match = re.search(r"https?://www\.deezer\.com/track/[0-9]+", html)
            if deezer_match:
                links["deezer"] = self._normalize_deezer_url(deezer_match.group(0))

            amazon_match = re.search(r"trackAsin=([A-Z0-9]{10})", html)
            if amazon_match:
                links["amazonMusic"] = self._normalize_amazon_url(f"https://music.amazon.com/tracks/{amazon_match.group(1)}?musicTerritory=US")
            tidal_match = re.search(r"https?://listen\.tidal\.com/track/[0-9]+", html)
            if tidal_match:
                links["tidal"] = tidal_match.group(0)
        except Exception as e:
            logger.debug(f"[link_resolver] Song.link HTML fallback async failed: {e}")
        return links

    async def _get_songlink_isrc_links_async(self, isrc: str) -> Dict[str, str]:
        try:
            params = {"isrc": isrc.upper().strip(), "userCountry": "US"}
            data = await self._safe_get_json(self.SONGLINK_API_URL, params=params)
            return self._process_songlink_response(data)
        except Exception as e:
            logger.debug(f"[link_resolver] Songlink ISRC lookup async failed: {e}")
        return {}

    async def _get_songstats_links_async(self, identifier: str) -> Dict[str, str]:
        try:
            url = f"https://songstats.com/{urllib.parse.quote(identifier)}?ref=ISRCFinder"
            resp = await self._safe_get_html(url)
            return self._process_songstats_links(resp.text)
        except Exception as e:
            logger.debug(f"[link_resolver] Songstats lookup async failed: {e}")
        return {}

    async def resolve_all_async(self, track_id: str, isrc: Optional[str] = None) -> Dict[str, str]:
        platform = "spotify"
        raw_id = track_id

        if track_id.startswith("apple_"):
            platform, raw_id = "appleMusic", track_id.replace("apple_", "")
        elif track_id.startswith("tidal_"):
            platform, raw_id = "tidal", track_id.replace("tidal_", "")
        elif track_id.startswith("deezer_"):
            platform, raw_id = "deezer", track_id.replace("deezer_", "")
        else:
            raw_id = track_id.replace("spotify_", "")

        links = {}

        if isrc:
            deezer_url = await self._get_deezer_url_by_isrc_async(isrc)
            if deezer_url:
                links["deezer"] = deezer_url
                logger.debug(f"[link_resolver] Found Deezer URL via ISRC async: {deezer_url}")

        try:
            songlink_links = {}
            if links.get("deezer"):
                songlink_links = await self._get_songlink_links_by_url_async(links["deezer"])
            else:
                songlink_links = await self._get_songlink_links_by_id_async(raw_id, platform)

            self._merge_links(links, songlink_links)
        except Exception as e:
            logger.debug(f"[link_resolver] Songlink async failed: {e}")

        if not isrc and links.get("deezer"):
            isrc = await self._get_isrc_from_deezer_async(links["deezer"])
            logger.debug(f"[link_resolver] ISRC retrieved via reverse lookup async: {isrc}")

        if isrc and (not links.get("tidal") or not links.get("amazonMusic") or not links.get("deezer")):
            logger.debug("[link_resolver] Triggering fallback resolvers async")

            if not links.get("deezer"):
                deezer_url = await self._get_deezer_url_by_isrc_async(isrc)
                if deezer_url:
                    links["deezer"] = deezer_url

            if not links.get("tidal") or not links.get("amazonMusic"):
                self._merge_links(links, await self._get_songlink_isrc_links_async(isrc))

            if not links.get("tidal") or not links.get("amazonMusic"):
                self._merge_links(links, await self._get_songstats_links_async(isrc))

        if (not links.get("tidal") or not links.get("amazonMusic")) and raw_id:
            html_links = await self._get_songlink_html_links_async(raw_id)
            for plat, url in html_links.items():
                if plat not in links and url:
                    links[plat] = url

        if isrc:
            links["isrc"] = isrc

        return links

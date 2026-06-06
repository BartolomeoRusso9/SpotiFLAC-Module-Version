import json
import logging
import urllib.parse
import functools
import re
from typing import Dict, Optional

from .http import HttpClient, songlink_rate_limiter

logger = logging.getLogger(__name__)

class LinkResolver:
    """Resolves cross-platform links using a Multi-Provider approach (Go style)."""

    SONGLINK_API_URL = "https://api.song.link/v1-alpha.1/links"
    DEEZER_ISRC_API = "https://api.deezer.com/track/isrc:{}"
    DEEZER_TRACK_API = "https://api.deezer.com/track/{}"

    _SONGLINK_PLATFORMS = (
        "deezer",
        "amazonMusic",
        "tidal",
        "appleMusic",
        "spotify",
        "soundcloud",
    )

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def identify_provider(self, url: str) -> str:
        """Identifies the platform directly from the provided URL."""
        url = url.lower()
        if "soundcloud.com" in url or "on.soundcloud.com" in url:
            return "soundcloud"
        elif "spotify.com" in url:
            return "spotify"
        return "unknown"
    
    # --- 1. URL NORMALIZATION ---
    
    def _normalize_amazon_url(self, raw_url: str) -> str:
        """Normalizes Amazon Music URLs to prevent regional blocks (appends US)."""
        url = raw_url.strip()
        if not url:
            return ""

        if "trackAsin=" in url:
            parts = url.split("trackAsin=")
            if len(parts) > 1:
                track_asin = parts[1].split("&")[0]
                if track_asin:
                    return f"https://music.amazon.com/tracks/{track_asin}?musicTerritory=US"

        # Match Go style regex
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

    # --- 2. ISRC MANAGEMENT & REVERSE LOOKUP ---

    def _get_isrc_from_deezer(self, deezer_url: str) -> str:
        """Performs a reverse lookup to fetch the ISRC from Deezer if missing."""
        track_id = self._extract_deezer_id(deezer_url)
        if not track_id: 
            return ""
        
        try:
            url = self.DEEZER_TRACK_API.format(track_id)
            data = self.http.get_json(url)
            isrc = data.get("isrc", "")
            if isrc:
                logger.debug(f"[link_resolver] Reverse ISRC fetched from Deezer: {isrc}")
                return isrc.upper().strip()
        except Exception as e:
            logger.debug(f"[link_resolver] Error during ISRC reverse lookup on Deezer: {e}")
        return ""

    @functools.lru_cache(maxsize=1024)
    def _get_deezer_url_by_isrc(self, isrc: str) -> str:
        """Searches for a track on Deezer using its ISRC."""
        try:
            url = self.DEEZER_ISRC_API.format(isrc.upper().strip())
            data = self.http.get_json(url)
            
            if "link" in data and data["link"]:
                return self._normalize_deezer_url(data["link"])
            elif "id" in data and data["id"] > 0:
                return f"https://www.deezer.com/track/{data['id']}"
        except Exception as e:
            logger.debug(f"[link_resolver] Deezer ISRC lookup failed: {e}")
        return ""

    # --- 3. MULTI-PROVIDER INTEGRATION (CHAIN) ---

    def _process_songlink_response(self, data: dict) -> Dict[str, str]:
        """Extracts and normalizes links received from Songlink."""
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

    def _get_songlink_links(self, params: dict[str, str]) -> dict[str, str]:
        try:
            songlink_rate_limiter.wait_for_slot()
            data = self.http.get_json(self.SONGLINK_API_URL, params=params)
            return self._process_songlink_response(data)
        except Exception as e:
            logger.debug(f"[link_resolver] Songlink lookup failed: {e}")
        return {}

    def _get_songlink_links_by_url(self, url: str) -> dict[str, str]:
        return self._get_songlink_links({"url": url, "userCountry": "US"})

    def _get_songlink_links_by_id(self, raw_id: str, platform: str) -> dict[str, str]:
        return self._get_songlink_links({"id": raw_id, "platform": platform, "userCountry": "US"})

    def _process_songstats_links(self, html: str) -> Dict[str, str]:
        links = {"amazonMusic": "", "tidal": "", "deezer": ""}
        matches = re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
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

    def _get_songlink_html_links(self, raw_id: str) -> Dict[str, str]:
        links: dict[str, str] = {}
        try:
            songlink_rate_limiter.wait_for_slot()
            url = f"https://song.link/s/{urllib.parse.quote(raw_id, safe='')}?userCountry=US"
            resp = self.http.get(url)
            html = resp.text

            deezer_match = re.search(r"https?://www\.deezer\.com/track/[0-9]+", html)
            if deezer_match:
                links["deezer"] = self._normalize_deezer_url(deezer_match.group(0))

            amazon_match = re.search(r"trackAsin=([A-Z0-9]{10})", html)
            if amazon_match:
                links["amazonMusic"] = self._normalize_amazon_url(
                    f"https://music.amazon.com/tracks/{amazon_match.group(1)}?musicTerritory=US"
                )
            tidal_match = re.search(r"https?://listen\.tidal\.com/track/[0-9]+", html)
            if tidal_match:
                links["tidal"] = tidal_match.group(0)
        except Exception as e:
            logger.debug(f"[link_resolver] Song.link HTML fallback failed: {e}")
        return links

    def _get_songlink_isrc_links(self, isrc: str) -> Dict[str, str]:
        try:
            songlink_rate_limiter.wait_for_slot()
            params = {"isrc": isrc.upper().strip(), "userCountry": "US"}
            data = self.http.get_json(self.SONGLINK_API_URL, params=params)
            return self._process_songlink_response(data)
        except Exception as e:
            logger.debug(f"[link_resolver] Songlink ISRC lookup failed: {e}")
        return {}

    def _get_songstats_links(self, identifier: str) -> Dict[str, str]:
        try:
            url = f"https://songstats.com/{urllib.parse.quote(identifier)}?ref=ISRCFinder"
            resp = self.http.get(url)
            return self._process_songstats_links(resp.text)
        except Exception as e:
            logger.debug(f"[link_resolver] Songstats lookup failed: {e}")
        return {}

    def resolve_all(self, track_id: str, isrc: Optional[str] = None) -> Dict[str, str]:
        """
        Resolves links by executing a Multi-Provider Resolver Chain.
        If the ISRC is missing, attempts to reverse lookup it mid-process.
        """
        platform = "spotify"
        raw_id = track_id

        # Dynamic provider recognition
        if track_id.startswith("apple_"):
            platform = "appleMusic"
            raw_id = track_id.replace("apple_", "")
        elif track_id.startswith("tidal_"):
            platform = "tidal"
            raw_id = track_id.replace("tidal_", "")
        elif track_id.startswith("deezer_"):
            platform = "deezer"
            raw_id = track_id.replace("deezer_", "")
        else:
            raw_id = track_id.replace("spotify_", "")

        links = {}

        # STEP 1: Direct Deezer resolution via ISRC (if available)
        if isrc:
            deezer_url = self._get_deezer_url_by_isrc(isrc)
            if deezer_url:
                links["deezer"] = deezer_url
                logger.debug(f"[link_resolver] Found Deezer URL via ISRC: {deezer_url}")

        # STEP 2: Songlink as primary resolver
        try:
            songlink_links = {}
            if links.get("deezer"):
                songlink_links = self._get_songlink_links_by_url(links["deezer"])
            else:
                songlink_links = self._get_songlink_links_by_id(raw_id, platform)

            self._merge_links(links, songlink_links)
        except Exception as e:
            logger.debug(f"[link_resolver] Songlink failed: {e}")

        # STEP 3: ISRC Reverse Lookup
        # If we started without ISRC (e.g. Spotify ID only) but Songlink found a Deezer URL,
        # we steal the ISRC from Deezer to use it in upcoming steps or for metadata.
        if not isrc and links.get("deezer"):
            isrc = self._get_isrc_from_deezer(links["deezer"])
            logger.debug(f"[link_resolver] ISRC retrieved via reverse lookup: {isrc}")

        # STEP 4: Fallback Resolver Chain (e.g. Songstats, Qobuz)
        # If Tidal or Amazon are still missing, but we now have the ISRC, trigger secondary resolvers.
        if isrc and (not links.get("tidal") or not links.get("amazonMusic") or not links.get("deezer")):
            logger.debug("[link_resolver] Triggering fallback resolvers")

            if not links.get("deezer"):
                deezer_url = self._get_deezer_url_by_isrc(isrc)
                if deezer_url:
                    links["deezer"] = deezer_url

            if not links.get("tidal") or not links.get("amazonMusic"):
                self._merge_links(links, self._get_songlink_isrc_links(isrc))

            if not links.get("tidal") or not links.get("amazonMusic"):
                self._merge_links(links, self._get_songstats_links(isrc))

        if (not links.get("tidal") or not links.get("amazonMusic")) and raw_id:
            html_links = self._get_songlink_html_links(raw_id)
            for plat, url in html_links.items():
                if plat not in links and url:
                    links[plat] = url

        # Store the discovered ISRC in the final dictionary (useful for metadata processing downstream)
        if isrc:
            links["isrc"] = isrc

        return links
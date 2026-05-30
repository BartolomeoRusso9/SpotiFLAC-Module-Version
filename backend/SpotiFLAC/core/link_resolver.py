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
        links = {}
        entities = data.get("linksByPlatform", {})
        
        if "deezer" in entities and entities["deezer"].get("url"):
            links["deezer"] = self._normalize_deezer_url(entities["deezer"]["url"])
        if "amazonMusic" in entities and entities["amazonMusic"].get("url"):
            links["amazonMusic"] = self._normalize_amazon_url(entities["amazonMusic"]["url"])
        if "tidal" in entities and entities["tidal"].get("url"):
            links["tidal"] = entities["tidal"]["url"].strip()
        # Add other platforms here if needed (e.g. qobuz)
            
        return links

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
                # Use heavily cached Deezer link to bypass strict rate limits
                safe_url = urllib.parse.quote(links["deezer"])
                params = {"url": safe_url, "userCountry": "US"}
                data = self.http.get_json(self.SONGLINK_API_URL, params=params)
                songlink_links = self._process_songlink_response(data)
            else:
                # Fallback to direct ID query (Subject to Rate Limits)
                songlink_rate_limiter.wait_for_slot()
                params = {"id": raw_id, "platform": platform, "userCountry": "US"}
                data = self.http.get_json(self.SONGLINK_API_URL, params=params)
                songlink_links = self._process_songlink_response(data)

            # Merge discovered links
            for plat, url in songlink_links.items():
                if plat not in links and url:
                    links[plat] = url

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
        if isrc and (not links.get("tidal") or not links.get("amazonMusic")):
            logger.debug("[link_resolver] Triggering fallback resolvers (e.g. Songstats/Qobuz)")
            # Implement calls to provider/songstats.py or provider/qobuz.py here
            pass

        # Store the discovered ISRC in the final dictionary (useful for metadata processing downstream)
        if isrc:
            links["isrc"] = isrc

        return links
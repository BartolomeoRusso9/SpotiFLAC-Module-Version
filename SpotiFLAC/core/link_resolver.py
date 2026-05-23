import logging
import urllib.parse
import functools
from typing import Dict

# Assumiamo che songlink_rate_limiter possa essere bypassato per le chiamate veloci
from .http import HttpClient, songlink_rate_limiter 

logger = logging.getLogger(__name__)

class LinkResolver:
    """Risolve link tra piattaforme usando l'approccio Multi-Provider (stile Go)."""

    SONGLINK_API_URL = "https://api.song.link/v1-alpha.1/links"
    DEEZER_ISRC_API = "https://api.deezer.com/track/isrc:{}"

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def identify_provider(self, url: str) -> str:
        """Identifica la piattaforma direttamente dall'URL fornito dall'utente."""
        url = url.lower()
        if "soundcloud.com" in url or "on.soundcloud.com" in url:
            return "soundcloud"
        elif "spotify.com" in url:
            return "spotify"
        return "unknown"
    
    @functools.lru_cache(maxsize=1024)
    def _get_deezer_url_by_isrc(self, isrc: str) -> str:
        """
        Cerca la traccia su Deezer tramite ISRC (Istantaneo e senza rate-limit severi).
        Replicato da lookupDeezerTrackURLByISRC del codice Go.
        """
        try:
            url = self.DEEZER_ISRC_API.format(isrc.upper().strip())
            data = self.http.get_json(url)
            
            # Deezer restituisce il link diretto o l'ID
            if "link" in data and data["link"]:
                return data["link"]
            elif "id" in data and data["id"] > 0:
                return f"https://www.deezer.com/track/{data['id']}"
        except Exception as e:
            logger.debug("[link_resolver] Deezer ISRC lookup failed: %s", e)
        return ""

    def resolve_all(self, track_id: str, isrc: str = None) -> Dict[str, str]:
        """
        Ritorna un dizionario con i link per ogni piattaforma.
        Se viene fornito l'ISRC, utilizza la logica rapida a cascata.
        """
        platform = "spotify"
        raw_id = track_id

        # Riconosce dinamicamente la piattaforma di partenza dal prefisso
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
        deezer_url = ""

        # STEP 1: Se abbiamo l'ISRC (come nel codice Go), risolviamo istantaneamente tramite Deezer
        if isrc:
            deezer_url = self._get_deezer_url_by_isrc(isrc)
            if deezer_url:
                links["deezer"] = deezer_url
                logger.debug(f"[link_resolver] Trovato Deezer URL tramite ISRC: {deezer_url}")

        # STEP 2: Usiamo Songlink per riempire i buchi (es. Tidal, Amazon)
        try:
            if deezer_url:
                # Metodo Go-style: Cerchiamo su Songlink usando l'URL di Deezer. 
                # Questo evita i rate-limit stretti sugli ID ed è pesantemente tenuto in cache.
                safe_url = urllib.parse.quote(deezer_url)
                params = {"url": safe_url, "userCountry": "US"}
                
                # NESSUN rate limiter necessario per chiamate URL cacheate
                data = self.http.get_json(self.SONGLINK_API_URL, params=params)
            else:
                # Metodo classico: Ricerca per ID diretto.
                params = {
                    "id": raw_id,
                    "platform": platform,
                    "userCountry": "US"
                }
                
                # IL RATE LIMITER INTERVIENE SOLO QUI come ultima spiaggia
                songlink_rate_limiter.wait_for_slot()
                data = self.http.get_json(self.SONGLINK_API_URL, params=params)

            # Popoliamo il dizionario saltando le chiavi che abbiamo già (come Deezer)
            entities = data.get("linksByPlatform", {})
            for plat, info in entities.items():
                if plat not in links and info.get("url"):
                    links[plat] = info.get("url")

        except Exception as e:
            logger.debug("[link_resolver] Odesli failed: %s", e)

        return links
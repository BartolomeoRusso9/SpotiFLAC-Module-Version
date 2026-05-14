import logging
from typing import Dict

# 1. IMPORTA IL RATE LIMITER DA http.py
from .http import HttpClient, songlink_rate_limiter

logger = logging.getLogger(__name__)

class LinkResolver:
    """Risolve link tra piattaforme usando Odesli (Songlink)."""

    API_URL = "https://api.song.link/v1-alpha.1/links"

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    # --- NUOVO METODO AGGIUNTO ---
    def identify_provider(self, url: str) -> str:
        """Identifica la piattaforma direttamente dall'URL fornito dall'utente."""
        url = url.lower()
        if "soundcloud.com" in url or "on.soundcloud.com" in url:
            return "soundcloud"
        elif "spotify.com" in url:
            return "spotify"
        return "unknown"
    # -----------------------------

    def resolve_all(self, track_id: str) -> Dict[str, str]:
        """Ritorna un dizionario con i link per ogni piattaforma riconoscendo la sorgente."""
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

        params = {
            "id": raw_id,
            "platform": platform,
            "userCountry": "US"
        }

        links = {}
        try:
            # 2. METTI IN ATTESA IL THREAD FINCHÈ NON C'È UNO SLOT LIBERO
            songlink_rate_limiter.wait_for_slot()

            # Ora la richiesta è sicura e non supererà le 9 al minuto!
            data = self.http.get_json(self.API_URL, params=params)

            entities = data.get("linksByPlatform", {})
            for plat, info in entities.items():
                links[plat] = info.get("url")
        except Exception as e:
            logger.debug("[link_resolver] Odesli failed: %s", e)
        return links
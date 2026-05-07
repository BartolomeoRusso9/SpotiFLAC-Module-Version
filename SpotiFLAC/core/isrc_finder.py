import logging
from typing import Optional
from .http import HttpClient

logger = logging.getLogger(__name__)

BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

def spotify_id_to_gid(spotify_id: str) -> str:
    """Converte un ID Spotify Base62 in un GID esadecimale da 32 caratteri."""
    res = 0
    for char in spotify_id:
        if char not in BASE62:
            raise ValueError(f"ID non-Spotify ignorato: carattere '{char}' non valido in Base62.")
        res = res * 62 + BASE62.index(char)
    return f"{res:032x}"

class IsrcFinder:
    """Ricerca ISRC tramite i mirror GID di Spotify."""

    def __init__(self, http_client: HttpClient):
        self.http = http_client

    def find_isrc(self, track_id: str) -> Optional[str]:
        try:
            gid = spotify_id_to_gid(track_id)
        except ValueError as e:
            logger.debug("[isrc_finder] %s", e)
            return None # Ignora in modo sicuro ID come 'apple_1588744445' o 'tidal_123'

        # Esempio di endpoint mirror (basato sulla logica Go)
        url = f"https://spclient.wg.spotify.com/metadata/4/track/{gid}"
        try:
            # Nota: richiede headers specifici o token anonimo
            data = self.http.get_json(url)
            return data.get("external_id", [{}])[0].get("value")
        except Exception as e:
            logger.debug("[isrc_finder] Mirror lookup failed: %s", e)
            return None
import re

from .isrc_cache import get_cached_isrc, put_cached_isrc
from .isrc_finder import IsrcFinder
from .link_resolver import LinkResolver

class IsrcHelper:
    """Gestore centralizzato per la risoluzione ISRC con fallback e traduzione cross-platform."""

    def __init__(self, http_client):
        from ..providers.songstats import SongstatsProvider
        from ..providers.soundplate import SoundplateProvider
        self.http = http_client
        self.finder = IsrcFinder(http_client)
        self.soundplate = SoundplateProvider(http_client)
        self.songstats = SongstatsProvider(http_client)
        self.resolver = LinkResolver(http_client)  # Inizializziamo il resolver

    def get_isrc(self, track_id: str) -> str:
        # 1. Cache
        cached = get_cached_isrc(track_id)
        if cached: return cached

        isrc = None
        search_id = track_id

        # 1.5. Traduzione ID (Se non è Spotify, cerchiamo il link Spotify tramite Odesli)
        if not track_id.startswith("spotify_") and "_" in track_id:
            try:
                links = self.resolver.resolve_all(track_id)
                spotify_url = links.get("spotify")
                if spotify_url:
                    match = re.search(r"track/([a-zA-Z0-9]{22})", spotify_url)
                    if match:
                        search_id = match.group(1)
            except Exception:
                pass  # Fallimento silenzioso, proseguiamo col normale flusso

        # 2. Sequenza di risoluzione (usando l'ID originale o quello tradotto)
        isrc = self.finder.find_isrc(search_id)
        if not isrc:
            isrc = self.soundplate.get_isrc(search_id)
        if not isrc:
            isrc = self.songstats.get_isrc(search_id)

        # 3. Salvataggio
        if isrc:
            put_cached_isrc(track_id, isrc)
            return isrc

        return ""
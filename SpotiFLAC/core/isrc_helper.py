from typing import Optional
from .isrc_cache import get_cached_isrc, put_cached_isrc
from ..providers.soundplate import SoundplateProvider
from ..providers.songstats import SongstatsProvider
from .isrc_finder import IsrcFinder

class IsrcHelper:
    """Gestore centralizzato per la risoluzione ISRC con fallback."""

    def __init__(self, http_client):
        self.finder = IsrcFinder(http_client)
        self.soundplate = SoundplateProvider(http_client)
        self.songstats = SongstatsProvider(http_client)

    def get_isrc(self, track_id: str) -> str:
        # 1. Cache
        cached = get_cached_isrc(track_id)
        if cached: return cached

        # 2. Sequenza di risoluzione
        isrc = self.finder.find_isrc(track_id)
        if not isrc:
            isrc = self.soundplate.get_isrc(track_id)
        if not isrc:
            isrc = self.songstats.get_isrc(track_id)

        # 3. Salvataggio
        if isrc:
            put_cached_isrc(track_id, isrc)
            return isrc
        return ""
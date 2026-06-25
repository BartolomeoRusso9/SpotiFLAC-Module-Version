import re

from .isrc_cache import get_cached_isrc_async, put_cached_isrc_async
from .isrc_finder import IsrcFinder
from .link_resolver import LinkResolver


class IsrcHelper:
    """Centralized handler for ISRC resolution with fallback and cross-platform translation."""

    def __init__(self, http_client):
        from ..providers.songstats import SongstatsProvider
        from ..providers.soundplate import SoundplateProvider

        self.http = http_client
        self.finder = IsrcFinder(http_client)
        self.soundplate = SoundplateProvider(http_client)
        self.songstats = SongstatsProvider(http_client)
        self.resolver = LinkResolver(http_client)

    async def get_isrc_async(self, track_id: str) -> str:
        # 1. Cache
        cached = await get_cached_isrc_async(track_id)
        if cached:
            return cached

        isrc = None
        search_id = track_id

        # 1.5. Traduzione ID
        if not track_id.startswith("spotify_") and "_" in track_id:
            try:
                links = await self.resolver.resolve_all_async(track_id)
                spotify_url = links.get("spotify")
                if spotify_url:
                    match = re.search(r"track/([a-zA-Z0-9]{22})", spotify_url)
                    if match:
                        search_id = match.group(1)
            except Exception:
                pass  # Fallimento silenzioso, proseguiamo col normale flusso

        # 2. Sequenza di risoluzione async
        isrc = await self.finder.find_isrc_async(search_id)

        if not isrc:
            isrc = await self.soundplate.get_isrc_async(search_id)

        if not isrc:
            isrc = await self.songstats.get_isrc_async(search_id)

        # 3. Salvataggio
        if isrc:
            await put_cached_isrc_async(track_id, isrc)
            return isrc

        return ""

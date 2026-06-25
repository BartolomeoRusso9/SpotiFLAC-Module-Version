"""
Metadata Enrichment — Phase 2 asyncio migration.

Adds `enrich_metadata_async` that uses `asyncio.gather` with a global timeout instead of `ThreadPoolExecutor`. The original sync version is retained for backward compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .http import NetworkManager
from .isrc_utils import normalize_isrc

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_HTTP_TIMEOUT = 4
_GLOBAL_TIMEOUT = 6.0
_ENRICHMENT_CACHE_TTL = 3600.0
_ENRICHMENT_CACHE_MAX = 2000
_TIDAL_MAX_APIS = 10
_TIDAL_MAX_WORKERS = 5


def _run_async_sync(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if loop.is_running():
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# EnrichedMetadata (invariato)
# ---------------------------------------------------------------------------


@dataclass
class EnrichedMetadata:
    genre: str = ""
    label: str = ""
    bpm: int = 0
    explicit: bool = False
    upc: str = ""
    isrc: str = ""
    cover_url_hd: str = ""
    _sources: dict[str, str] = field(default_factory=dict, repr=False)

    def as_tags(self) -> dict[str, str]:
        tags: dict[str, str] = {}
        if self.genre:
            tags["GENRE"] = self.genre
        if self.label:
            tags["ORGANIZATION"] = self.label
        if self.bpm:
            tags["BPM"] = str(self.bpm)
        if self.upc:
            tags["UPC"] = self.upc
        if self.isrc:
            isrc_n = normalize_isrc(self.isrc)
            if isrc_n:
                tags["ISRC"] = isrc_n
        if self.explicit:
            tags["ITUNESADVISORY"] = "1"
        return tags

    def merge(self, other: "EnrichedMetadata", source: str) -> None:
        for attr in ("genre", "label", "bpm", "upc", "isrc", "cover_url_hd"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
                self._sources[attr] = source
        if not self.explicit and other.explicit:
            self.explicit = True
            self._sources["explicit"] = source

    def is_complete(self) -> bool:
        return bool(self.genre and self.label and self.cover_url_hd)


# ---------------------------------------------------------------------------
# In-memory cache (invariata)
# ---------------------------------------------------------------------------

_enrichment_cache: dict[str, tuple[EnrichedMetadata, float]] = {}
_cache_lock = threading.Lock()


def _get_cached(isrc: str) -> EnrichedMetadata | None:
    if not isrc:
        return None
    with _cache_lock:
        entry = _enrichment_cache.get(isrc.upper())
        if entry and (time.time() - entry[1]) < _ENRICHMENT_CACHE_TTL:
            return entry[0]
    return None


def _put_cached(isrc: str, data: EnrichedMetadata) -> None:
    if not isrc:
        return
    with _cache_lock:
        key = isrc.upper()
        _enrichment_cache[key] = (data, time.time())
        if len(_enrichment_cache) > _ENRICHMENT_CACHE_MAX:
            oldest_key = min(_enrichment_cache.items(), key=lambda kv: kv[1][1])[0]
            try:
                _enrichment_cache.pop(oldest_key, None)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Sync provider classes (invariate)
# ---------------------------------------------------------------------------


class _DeezerMeta:
    BASE = "https://api.deezer.com/2.0"

    def __init__(self) -> None:
        self._client = None

    def fetch(self, isrc: str) -> EnrichedMetadata:
        return _run_async_sync(self.fetch_async(isrc))

    async def fetch_async(self, isrc: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        if not isrc:
            return out
        try:
            client = await NetworkManager.get_async_client_safe()
            r = await client.get(
                f"{self.BASE}/track/isrc:{isrc}",
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": _UA},
            )
            if r.status_code != 200:
                return out
            d = r.json()
            if "error" in d:
                return out
            album_id = d.get("album", {}).get("id")
            if album_id:
                ar = await client.get(
                    f"{self.BASE}/album/{album_id}",
                    timeout=_HTTP_TIMEOUT,
                    headers={"User-Agent": _UA},
                )
                if ar.is_success:
                    ad = ar.json()
                    genres = ad.get("genres", {}).get("data", [])
                    if genres:
                        out.genre = genres[0].get("name", "")
                    out.label = ad.get("label", "")
                    out.upc = ad.get("upc", "")
                    out.cover_url_hd = ad.get("cover_xl") or ad.get("cover_big", "")
            out.bpm = int(d.get("bpm") or 0)
            out.explicit = bool(d.get("explicit_lyrics"))
            out.isrc = d.get("isrc", "")
        except Exception as exc:
            logger.debug("[meta/deezer] async %s", exc)
        return out


class _AppleMusicMeta:
    SEARCH = "https://itunes.apple.com/search"

    def __init__(self) -> None:
        self._client = None

    def fetch(
        self, track_name: str, artist_name: str, isrc: str = ""
    ) -> EnrichedMetadata:
        return _run_async_sync(self.fetch_async(track_name, artist_name, isrc))

    def _search(self, title: str, artist: str, isrc: str) -> dict[str, Any] | None:
        return _run_async_sync(self._search_async(title, artist, isrc))

    async def _search_async(
        self, title: str, artist: str, isrc: str
    ) -> dict[str, Any] | None:
        try:
            client = await NetworkManager.get_async_client_safe()
            if isrc:
                r = await client.get(
                    self.SEARCH,
                    params={
                        "term": isrc,
                        "media": "music",
                        "entity": "song",
                        "limit": 1,
                        "country": "US",
                    },
                    headers={"User-Agent": _UA},
                    timeout=_HTTP_TIMEOUT,
                )
                if r.is_success:
                    results = r.json().get("results", [])
                    if results:
                        return results[0]
            r = await client.get(
                self.SEARCH,
                params={
                    "term": f"{title} {artist}",
                    "media": "music",
                    "entity": "song",
                    "limit": 5,
                    "country": "US",
                },
                headers={"User-Agent": _UA},
                timeout=_HTTP_TIMEOUT,
            )
            if not r.is_success:
                return None
            results = r.json().get("results", [])
            if not results:
                return None
            artist_lc = artist.lower()
            for item in results:
                if artist_lc in item.get("artistName", "").lower():
                    return item
            return results[0]
        except Exception as exc:
            logger.debug("[meta/apple] async %s", exc)
            return None

    async def fetch_async(
        self, track_name: str, artist_name: str, isrc: str = ""
    ) -> EnrichedMetadata:
        out = EnrichedMetadata()
        item = await self._search_async(track_name, artist_name, isrc)
        if not item:
            return out
        out.genre = item.get("primaryGenreName", "")
        out.explicit = item.get("trackExplicitness") == "explicit"
        raw_art = item.get("artworkUrl100", "")
        out.cover_url_hd = raw_art.replace("100x100", "600x600")
        return out


_TIDAL_APIS_BUILTIN: list[str] = []


class _TidalMeta:
    def __init__(self) -> None:
        self._client = None
        self._apis: list[str] = []
        self._apis_ready = False
        self._apis_lock = threading.Lock()
        self._load_apis_from_cache()

    def _load_apis_from_cache(self) -> None:
        try:
            from ..providers.tidal import get_tidal_api_list

            apis = get_tidal_api_list()
            if apis:
                self._apis = apis
                self._apis_ready = True
                return
        except Exception:
            pass
        self._apis = list(_TIDAL_APIS_BUILTIN)
        self._apis_ready = True
        threading.Thread(target=self._refresh_bg, daemon=True).start()

    def _refresh_bg(self) -> None:
        try:
            from ..providers.tidal import refresh_tidal_api_list

            apis = refresh_tidal_api_list(force=False)
            if apis:
                with self._apis_lock:
                    self._apis = apis
        except Exception as exc:
            logger.debug("[meta/tidal] refresh background failed: %s", exc)

    def fetch(self, track_name: str, artist_name: str) -> EnrichedMetadata:
        return _run_async_sync(self.fetch_async(track_name, artist_name))

    def _try_api(self, api: str, query: str) -> dict | None:
        return _run_async_sync(self._try_api_async(api, query))

    def _search_parallel(self, title: str, artist: str) -> dict | None:
        return _run_async_sync(self._search_parallel_async(title, artist))

    async def _try_api_async(self, api: str, query: str) -> dict | None:
        base = api.rstrip("/")
        client = await NetworkManager.get_async_client_safe()
        for endpoint in (
            f"{base}/search/?s={query}&limit=3",
            f"{base}/search?s={query}&limit=3",
        ):
            try:
                r = await client.get(
                    endpoint, timeout=_HTTP_TIMEOUT, headers={"User-Agent": _UA}
                )
                if not r.is_success:
                    continue
                data = r.json()
                items = (
                    data
                    if isinstance(data, list)
                    else data.get("tracks", {}).get("items", [])
                )
                if items:
                    return items[0]
            except Exception:
                pass
        return None

    async def _search_parallel_async(self, title: str, artist: str) -> dict | None:
        from urllib.parse import quote

        clean = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip() or title
        first = artist.split(",")[0].strip()
        query = quote(f"{first} {clean}")

        with self._apis_lock:
            apis = list(self._apis)

        apis_to_try = apis[:_TIDAL_MAX_APIS]
        if not apis_to_try:
            return None

        async def _one(api: str) -> dict | None:
            return await self._try_api_async(api, query)

        tasks = [asyncio.create_task(_one(api)) for api in apis_to_try]
        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    data = await coro
                    if data:
                        return data
                except Exception:
                    pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        return None

    async def fetch_async(self, track_name: str, artist_name: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        track_data = await self._search_parallel_async(track_name, artist_name)
        if not track_data:
            return out
        album = track_data.get("album", {})
        out.cover_url_hd = album.get("cover", "")
        out.explicit = bool(track_data.get("explicit"))
        out.isrc = track_data.get("isrc", "")
        return out


class _QobuzMeta:
    def __init__(self, qobuz_token: str | None = None) -> None:
        self._provider: Any = None
        self._qobuz_token = qobuz_token

    def _get_provider(self) -> Any:
        if self._provider is None:
            try:
                from ..providers.qobuz import QobuzProvider

                self._provider = QobuzProvider(qobuz_token=self._qobuz_token)
            except Exception as exc:
                logger.debug("[meta/qobuz] cannot init provider: %s", exc)
        return self._provider

    def fetch(self, isrc: str) -> EnrichedMetadata:
        return _run_async_sync(self.fetch_async(isrc))

    async def fetch_async(self, isrc: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        if not isrc:
            return out
        try:
            prov = self._get_provider()
            if prov is None:
                return out
            if hasattr(prov, "_search_by_isrc_async"):
                track = await prov._search_by_isrc_async(isrc)
            else:
                track = None
            if not track:
                return out
            album = track.get("album", {})
            out.genre = (album.get("genre", {}) or {}).get("name", "")
            out.label = (
                album.get("label", {}).get("name", "")
                if isinstance(album.get("label"), dict)
                else ""
            )
            out.cover_url_hd = album.get("image", {}).get("large", "")
            out.explicit = bool(track.get("parental_warning"))
            out.isrc = track.get("isrc", "")
            out.upc = album.get("upc", "")
        except Exception as exc:
            logger.debug("[meta/qobuz] async %s", exc)
        return out


@functools.lru_cache(maxsize=2)
def _get_qobuz_meta(token: str | None) -> _QobuzMeta:
    return _QobuzMeta(qobuz_token=token)


class _SoundCloudMeta:
    def __init__(self) -> None:
        self._provider: Any = None
        self._init_attempted = False

    def _get_provider(self) -> Any:
        if self._init_attempted:
            return self._provider
        self._init_attempted = True
        try:
            from ..providers.soundcloud import SoundCloudProvider

            self._provider = SoundCloudProvider()
        except Exception as exc:
            logger.debug("[meta/soundcloud] cannot init provider: %s", exc)
        return self._provider

    def fetch(self, track_name: str, artist_name: str) -> EnrichedMetadata:
        return _run_async_sync(self.fetch_async(track_name, artist_name))

    async def fetch_async(self, track_name: str, artist_name: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        try:
            prov = self._get_provider()
            if prov is None:
                return out
            query = f"{artist_name} {track_name}".strip()
            data = await prov._api_get_async(
                "search/tracks",
                {"q": query, "limit": 1, "access": "playable"},
            )
            items = data.get("collection", []) if isinstance(data, dict) else []
            if not items:
                return out
            formatted = prov._format_track(items[0])
            if formatted:
                out.cover_url_hd = formatted.get("cover_url", "")
        except Exception as exc:
            logger.debug("[meta/soundcloud] async %s", exc)
        return out


# ---------------------------------------------------------------------------
# Singleton provider instances
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_deezer_inst: _DeezerMeta | None = None
_apple_inst: _AppleMusicMeta | None = None
_tidal_inst: _TidalMeta | None = None
_sc_inst: _SoundCloudMeta | None = None


def _get_deezer() -> _DeezerMeta:
    global _deezer_inst
    if _deezer_inst is None:
        with _singleton_lock:
            if _deezer_inst is None:
                _deezer_inst = _DeezerMeta()
    return _deezer_inst


def _get_apple() -> _AppleMusicMeta:
    global _apple_inst
    if _apple_inst is None:
        with _singleton_lock:
            if _apple_inst is None:
                _apple_inst = _AppleMusicMeta()
    return _apple_inst


def _get_tidal() -> _TidalMeta:
    global _tidal_inst
    if _tidal_inst is None:
        with _singleton_lock:
            if _tidal_inst is None:
                _tidal_inst = _TidalMeta()
    return _tidal_inst


def _get_sc() -> _SoundCloudMeta:
    global _sc_inst
    if _sc_inst is None:
        with _singleton_lock:
            if _sc_inst is None:
                _sc_inst = _SoundCloudMeta()
    return _sc_inst


# ---------------------------------------------------------------------------
# Async fetch wrappers per i provider sync (Phase 2)
# ---------------------------------------------------------------------------


async def _deezer_fetch_async(isrc: str) -> EnrichedMetadata:
    return await _get_deezer().fetch_async(isrc)


async def _apple_fetch_async(
    track_name: str, artist_name: str, isrc: str
) -> EnrichedMetadata:
    return await _get_apple().fetch_async(track_name, artist_name, isrc)


async def _tidal_fetch_async(track_name: str, artist_name: str) -> EnrichedMetadata:
    return await _get_tidal().fetch_async(track_name, artist_name)


async def _qobuz_fetch_async(isrc: str, qobuz_token: str | None) -> EnrichedMetadata:
    return await _get_qobuz_meta(qobuz_token).fetch_async(isrc)


async def _soundcloud_fetch_async(
    track_name: str, artist_name: str
) -> EnrichedMetadata:
    return await _get_sc().fetch_async(track_name, artist_name)


# ---------------------------------------------------------------------------
# Async enrich_metadata — Phase 2 (nuovo)
# ---------------------------------------------------------------------------


async def enrich_metadata_async(
    track_name: str,
    artist_name: str,
    isrc: str = "",
    providers: list[str] | None = None,
    timeout_s: float = _GLOBAL_TIMEOUT,
    qobuz_token: str | None = None,
) -> EnrichedMetadata:
    """
    Queries providers in parallel with asyncio.gather + global timeout.
    Replaces the sync version's ThreadPoolExecutor.
    """
    if providers is None:
        providers = ["deezer", "apple", "qobuz", "tidal"]

    if isrc:
        cached = _get_cached(isrc)
        if cached is not None:
            return cached

    async def run_provider(name: str) -> tuple[str, EnrichedMetadata]:
        try:
            if name == "deezer":
                return name, await _deezer_fetch_async(isrc)
            elif name == "apple":
                return name, await _apple_fetch_async(track_name, artist_name, isrc)
            elif name == "tidal":
                return name, await _tidal_fetch_async(track_name, artist_name)
            elif name == "qobuz":
                return name, await _qobuz_fetch_async(isrc, qobuz_token)
            elif name == "soundcloud":
                return name, await _soundcloud_fetch_async(track_name, artist_name)
            else:
                logger.warning("[meta/enrich] provider sconosciuto: %s", name)
                return name, EnrichedMetadata()
        except Exception as exc:
            logger.debug("[meta/enrich] %s failed: %s", name, exc)
            return name, EnrichedMetadata()

    try:
        results_raw = await asyncio.wait_for(
            asyncio.gather(*[run_provider(p) for p in providers]),
            timeout=timeout_s,
        )
        results = dict(results_raw)
    except asyncio.TimeoutError:
        logger.warning("[meta/enrich] async timeout %.1fs", timeout_s)
        results = {}

    merged = EnrichedMetadata()
    for name in providers:
        if name in results:
            data = results[name]
            if isinstance(data, EnrichedMetadata):
                merged.merge(data, name)
            if merged.is_complete():
                break

    if merged._sources:
        logger.debug("[meta/enrich] async enriched: %s", merged._sources)

    if isrc and (merged.genre or merged.label or merged.cover_url_hd):
        _put_cached(isrc, merged)

    return merged

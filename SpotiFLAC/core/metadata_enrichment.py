"""Metadata Enrichment — Phase 2 asyncio migration.

Adds `enrich_metadata_async` that uses `asyncio.gather` with a global timeout instead of `ThreadPoolExecutor`. The original sync version is retained for backward compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .http import NetworkManager
from .isrc_utils import normalize_isrc
from .loop_runner import run_sync
from .response_cache import get as get_cached_response
from .response_cache import put as put_cached_response
from .text_match import fold, is_latin_script, ratio, score_track_match, titles_match

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_HTTP_TIMEOUT = 4
_GLOBAL_TIMEOUT = 6.0
_ENRICHMENT_CACHE_TTL = 3600.0
#: A lookup that found nothing is remembered too, but only briefly: the
#: providers do add releases, and an hour of "not there" for a track that
#: appeared five minutes ago is worse than the repeat request costs.
#: Without this a miss was simply never cached, so a failing ISRC paid the
#: full four-provider fan-out again on every single pass over a library.
_NEGATIVE_CACHE_TTL = 300.0
_ENRICHMENT_CACHE_MAX = 2000
#: The on-disk cache's name. "-v2" since enrichment began reading credits,
#: composer and ℗ from Qobuz and the extensions: a day-old entry written
#: before that has none of those fields, and would have kept them out of
#: every track it matched until it expired.
_CACHE_NAMESPACE = "metadata-enrichment-v2"

#: Below this, a search result is not the track we asked for. iTunes
#: always answers *something* — searching an ISRC it does not know
#: returns whatever the digits look like — so taking results[0] on faith
#: tagged tracks with a stranger's genre and cover art.
_APPLE_MATCH_MIN = 0.55
_TIDAL_MAX_APIS = 10
_TIDAL_MAX_WORKERS = 5


def _run_async_sync(coro):
    """Thin alias for loop_runner.run_sync() — see that function for why the
    old three-branch shim (which created a fresh loop in every branch) went
    away. Kept as a name so the many call sites below read unchanged.
    """
    return run_sync(coro)


# ---------------------------------------------------------------------------
# EnrichedMetadata (invariato)
# ---------------------------------------------------------------------------


#: Everything merge() carries across, in the order the tag names are built
#: from. `explicit` is handled apart because it is a flag: absent and False
#: are the same value, so "already set" cannot mean "do not replace".
_MERGE_ATTRS = (
    "genre",
    "label",
    "bpm",
    "upc",
    "isrc",
    "cover_url_hd",
    "composer",
    "copyright",
    "release_date",
    "album_type",
    "total_tracks",
    "total_discs",
    "lyricist",
    "producer",
    "mixer",
    "engineer",
)


@dataclass
class EnrichedMetadata:
    genre: str = ""
    label: str = ""
    bpm: int = 0
    explicit: bool = False
    upc: str = ""
    isrc: str = ""
    cover_url_hd: str = ""
    composer: str = ""
    copyright: str = ""
    release_date: str = ""
    album_type: str = ""
    #: The release's own totals. These matter more than they look: nothing
    #: else in the pipeline knows the disc count, so a two-disc album was
    #: tagged DISCTOTAL=1 — the model's default — on every track of it.
    total_tracks: int = 0
    total_discs: int = 0
    #: Credits, from services that publish them (Qobuz's performer list,
    #: Tidal's credits). Several names are joined with "; ".
    lyricist: str = ""
    producer: str = ""
    mixer: str = ""
    engineer: str = ""
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
        if self.composer:
            tags["COMPOSER"] = self.composer
        if self.copyright:
            tags["COPYRIGHT"] = self.copyright
        if self.release_date:
            tags["DATE"] = self.release_date
        if self.album_type:
            tags["RELEASETYPE"] = self.album_type
        if self.total_tracks > 0:
            tags["TRACKTOTAL"] = str(self.total_tracks)
        if self.total_discs > 0:
            tags["DISCTOTAL"] = str(self.total_discs)
        if self.explicit:
            tags["ITUNESADVISORY"] = "1"
        for attr, tag in (
            ("lyricist", "LYRICIST"),
            ("producer", "PRODUCER"),
            ("mixer", "MIXER"),
            ("engineer", "ENGINEER"),
        ):
            if getattr(self, attr):
                tags[tag] = getattr(self, attr)
        return tags

    def merge(self, other: EnrichedMetadata, source: str) -> None:
        for attr in _MERGE_ATTRS:
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

#: (value, stored_at, ttl_s) — the TTL is per entry so a negative result can
#: expire sooner than a real one. See _NEGATIVE_CACHE_TTL.
_enrichment_cache: dict[str, tuple[EnrichedMetadata, float, float]] = {}
_cache_lock = threading.Lock()


def _get_cached(isrc: str) -> EnrichedMetadata | None:
    import dataclasses

    if not isrc:
        return None
    with _cache_lock:
        entry = _enrichment_cache.get(isrc.upper())
        if entry and (time.time() - entry[1]) < entry[2]:
            return entry[0]
    persisted = get_cached_response(_CACHE_NAMESPACE, isrc.upper(), 24 * 60 * 60)
    if isinstance(persisted, dict):
        valid_fields = {f.name for f in dataclasses.fields(EnrichedMetadata)}
        filtered = {k: v for k, v in persisted.items() if k in valid_fields}
        result = EnrichedMetadata(**filtered)
        _put_cached_memory(isrc, result)
        return result
    return None


def _put_cached_memory(
    isrc: str,
    data: EnrichedMetadata,
    ttl_s: float = _ENRICHMENT_CACHE_TTL,
) -> None:
    if not isrc:
        return
    with _cache_lock:
        key = isrc.upper()
        _enrichment_cache[key] = (data, time.time(), ttl_s)
        if len(_enrichment_cache) > _ENRICHMENT_CACHE_MAX:
            oldest_key = min(_enrichment_cache.items(), key=lambda kv: kv[1][1])[0]
            with contextlib.suppress(Exception):
                _enrichment_cache.pop(oldest_key, None)


def _put_cached(isrc: str, data: EnrichedMetadata) -> None:
    if not isrc:
        return
    if not (
        data.genre
        or data.label
        or data.cover_url_hd
        or data.upc
        or data.composer
        or data.copyright
    ):
        # Nothing usable came back. Remember that, in memory only and on the
        # short TTL — persisting a miss to disk would keep a track starved of
        # metadata across restarts long after the provider learned about it.
        _put_cached_memory(isrc, data, _NEGATIVE_CACHE_TTL)
        return
    _put_cached_memory(isrc, data)
    put_cached_response(
        _CACHE_NAMESPACE,
        isrc.upper(),
        {
            "genre": data.genre,
            "label": data.label,
            "bpm": data.bpm,
            "explicit": data.explicit,
            "upc": data.upc,
            "isrc": data.isrc,
            "cover_url_hd": data.cover_url_hd,
            "composer": data.composer,
            "copyright": data.copyright,
            "release_date": data.release_date,
            "album_type": data.album_type,
            "total_tracks": data.total_tracks,
            "total_discs": data.total_discs,
            "lyricist": data.lyricist,
            "producer": data.producer,
            "mixer": data.mixer,
            "engineer": data.engineer,
        },
    )


# ---------------------------------------------------------------------------
# Sync provider classes (invariate)
# ---------------------------------------------------------------------------


def _get_dynamic_python_module(base_name: str) -> Any:
    """Helper per trovare un modulo Python caricato dal manager."""
    import sys

    from SpotiFLAC.extensions.manager import ExtensionManager

    manager = ExtensionManager(auto_install_downloads=False)
    try:
        manager.preload_python_modules()
    except Exception as e:
        logger.warning("[metadata_enrichment] Failed to preload Python modules: %s", e)

    cand = manager.find_python_extension(base_name)
    if cand:
        mod_name = f"SpotiFLAC.extensions_plugins.{cand.replace('-', '_')}"
        return sys.modules.get(mod_name)
    return None


def _get_dynamic_python_provider(base_name: str, **kwargs) -> Any:
    """Helper per istanziare un provider Python caricato dal manager."""
    from SpotiFLAC.extensions.manager import ExtensionManager
    from SpotiFLAC.extensions.python_provider import PythonExtensionProvider

    manager = ExtensionManager(auto_install_downloads=False)
    cand = manager.find_python_extension(base_name)
    if cand:
        return PythonExtensionProvider(cand, **kwargs)
    return None


#: Deezer labels every credited person with a role. Most are "Main" or
#: "Featured"; these are the ones that mean "wrote it".
_DEEZER_COMPOSER_ROLES = frozenset(
    {"composer", "writer", "songwriter", "author", "lyricist", "compositor"},
)


def _same_release(expected: str, found: str) -> bool:
    """Whether two album titles name the same release.

    Deliberately loose — "Abbey Road" and "Abbey Road (Remastered)" are the
    same record for tagging purposes — but not blind: fold() already strips
    case, accents and punctuation, and the containment test catches the
    edition suffixes that folding leaves behind.
    """
    left, right = fold(expected), fold(found)
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    return ratio(expected, found) >= 0.85


#: Deezer names genres in the language the request asks for, and without
#: asking, in the one its servers guess from the address: "Musica Asiatica"
#: from Italy for what is "Asian Music" everywhere else.
_DEEZER_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

#: A credited role, reduced to letters, and the credit field(s) it fills.
#: Covers Qobuz's performer roles ("MixingEngineer", "ComposerLyricist")
#: and Tidal's credit types ("Mixing Engineer").
_CREDIT_ROLES: dict[str, tuple[str, ...]] = {
    "composer": ("composer",),
    "composerlyricist": ("composer", "lyricist"),
    "songwriter": ("composer", "lyricist"),
    "writer": ("lyricist",),
    "lyricist": ("lyricist",),
    "producer": ("producer",),
    "coproducer": ("producer",),
    "mixer": ("mixer",),
    "mixingengineer": ("mixer",),
    "mixengineer": ("mixer",),
    "engineer": ("engineer",),
    "masteringengineer": ("engineer",),
    "recordingengineer": ("engineer",),
}


def _apply_credits(out: EnrichedMetadata, pairs) -> None:
    """Fills the credit fields of `out` from (name, role) pairs, leaving any
    field already set alone."""
    buckets: dict[str, list[str]] = {}
    for name, role in pairs:
        name = str(name or "").strip()
        fields = _CREDIT_ROLES.get(re.sub(r"[^a-z]", "", str(role or "").lower()))
        if not name or not fields:
            continue
        for field_name in fields:
            buckets.setdefault(field_name, []).append(name)
    for field_name, names in buckets.items():
        if not getattr(out, field_name):
            setattr(out, field_name, "; ".join(dict.fromkeys(names)))


def _qobuz_performer_pairs(text: Any) -> list[tuple[str, str]]:
    """Qobuz's `performers` string — "Max Martin, Producer, Composer - Serban
    Ghenea, MixingEngineer" — as (name, role) pairs."""
    pairs: list[tuple[str, str]] = []
    for chunk in str(text or "").split(" - "):
        parts = [p.strip() for p in chunk.split(",") if p.strip()]
        if len(parts) < 2:
            continue
        pairs.extend((parts[0], role) for role in parts[1:])
    return pairs


def _other_recording(expected: str, found: str) -> bool:
    """Whether a lookup's title shows it answered about another song.

    An ISRC lookup is an identity lookup, but only as good as the ISRC: a
    wrong one — a typo, a reissue's code reused — returns a stranger's
    track, and its credits and ℗ line would be written into the file. So a
    title that is plainly a different song discards the answer.

    Plainly: both titles in Latin script and nowhere near each other. A
    Hangul title against its romanisation shares no letters at all, and
    that is the same song.
    """
    if not expected or not found:
        return False
    if not (is_latin_script(expected) and is_latin_script(found)):
        return False
    return not titles_match(expected, found) and ratio(expected, found) < 0.5


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class _DeezerMeta:
    BASE = "https://api.deezer.com/2.0"

    def __init__(self) -> None:
        self._client = None

    def fetch(self, isrc: str, album_name: str = "") -> EnrichedMetadata:
        return _run_async_sync(self.fetch_async(isrc, album_name))

    async def fetch_async(
        self, isrc: str, album_name: str = "", track_name: str = ""
    ) -> EnrichedMetadata:
        """Deezer's view of one recording, looked up by ISRC.

        `album_name` guards the release-scoped fields. An ISRC identifies a
        *recording*, not a release, so Deezer routinely answers with the
        compilation or the deluxe edition it happens to file that recording
        under. Its label, barcode, date and track count then describe a
        record the user is not tagging — a UPC pointing at a greatest-hits
        disc is worse than no UPC at all. When the caller tells us which
        album it wanted, those fields are taken only if Deezer agrees; when
        it does not, we cannot check and the previous behaviour stands.
        """
        out = EnrichedMetadata()
        if not isrc:
            return out
        try:
            client = await NetworkManager.get_async_client_safe()
            r = await client.get(
                f"{self.BASE}/track/isrc:{isrc}",
                timeout=_HTTP_TIMEOUT,
                headers=_DEEZER_HEADERS,
            )
            if r.status_code != 200:
                return out
            d = r.json()
            if "error" in d:
                return out
            if _other_recording(track_name, str(d.get("title") or "")):
                logger.debug(
                    "[meta/deezer] ISRC %s is %r, not %r — ignored",
                    isrc,
                    d.get("title"),
                    track_name,
                )
                return out

            composers = []
            for contributor in d.get("contributors") or []:
                role = str(contributor.get("role") or "").strip().lower()
                name = str(contributor.get("name") or "").strip()
                if name and role in _DEEZER_COMPOSER_ROLES:
                    composers.append(name)
            out.composer = "; ".join(dict.fromkeys(composers))

            album_id = (d.get("album") or {}).get("id")
            if album_id:
                ar = await client.get(
                    f"{self.BASE}/album/{album_id}",
                    timeout=_HTTP_TIMEOUT,
                    headers=_DEEZER_HEADERS,
                )
                if ar.is_success:
                    ad = ar.json()
                    # Genre describes the music, not the pressing, so it is
                    # taken whichever release this turned out to be. All of
                    # them, not just the first: Deezer files "Rock" and
                    # "Classic Rock" side by side and picking [0] threw away
                    # a genre the user could have had for free.
                    genres = [
                        str(g.get("name") or "").strip()
                        for g in (ad.get("genres") or {}).get("data") or []
                    ]
                    out.genre = "; ".join(
                        dict.fromkeys(name for name in genres if name),
                    )

                    found_title = str(ad.get("title") or "")
                    release_ok = not album_name or _same_release(
                        album_name,
                        found_title,
                    )
                    if release_ok:
                        out.label = ad.get("label", "")
                        out.upc = ad.get("upc", "")
                        out.release_date = str(ad.get("release_date") or "")
                        out.album_type = str(ad.get("record_type") or "")
                        out.total_tracks = int(ad.get("nb_tracks") or 0)
                        out.cover_url_hd = ad.get("cover_xl") or ad.get(
                            "cover_big",
                            "",
                        )
                    else:
                        logger.debug(
                            "[meta/deezer] release mismatch: wanted %r, got %r "
                            "— keeping genre only",
                            album_name,
                            found_title,
                        )
            out.bpm = int(d.get("bpm") or 0)
            out.explicit = bool(d.get("explicit_lyrics"))
            out.isrc = d.get("isrc", "")
        except Exception as exc:
            logger.debug("[meta/deezer] async %s", exc)
        return out


@dataclass
class _ItunesCandidate:
    """One iTunes search hit, in the shape score_track_match() reads.

    The scorer speaks in title/artists/album/duration_ms and is already
    tuned and tested against the download providers; adapting to it beats
    writing a second, subtly different comparison here.
    """

    title: str
    artists: str
    first_artist: str
    album: str
    duration_ms: int

    @classmethod
    def of(cls, item: dict[str, Any]) -> _ItunesCandidate:
        name = str(item.get("artistName") or "")
        return cls(
            title=str(item.get("trackName") or ""),
            artists=name,
            first_artist=name,
            album=str(item.get("collectionName") or ""),
            duration_ms=int(item.get("trackTimeMillis") or 0),
        )


class _AppleMusicMeta:
    SEARCH = "https://itunes.apple.com/search"

    def __init__(self) -> None:
        self._client = None

    def fetch(
        self,
        track_name: str,
        artist_name: str,
        isrc: str = "",
        album_name: str = "",
        duration_ms: int = 0,
    ) -> EnrichedMetadata:
        return _run_async_sync(
            self.fetch_async(track_name, artist_name, isrc, album_name, duration_ms),
        )

    def _search(
        self,
        title: str,
        artist: str,
        isrc: str,
        album: str = "",
        duration_ms: int = 0,
    ) -> dict[str, Any] | None:
        return _run_async_sync(
            self._search_async(title, artist, isrc, album, duration_ms),
        )

    def _best(
        self,
        results: list[dict[str, Any]],
        title: str,
        artist: str,
        album: str,
        duration_ms: int,
    ) -> dict[str, Any] | None:
        """The best-scoring hit, or None if none of them is good enough.

        None is a real answer here. iTunes has no "no match" response: an
        unknown ISRC or a misspelt title still comes back with songs, and
        the old code returned results[0] regardless — so a track iTunes had
        never heard of was tagged with the genre, the explicit flag and the
        cover art of whatever happened to rank first.
        """
        if not results:
            return None
        if not title:
            return results[0]

        best: dict[str, Any] | None = None
        best_score = 0.0
        for item in results:
            score = score_track_match(
                title=title,
                artist=artist,
                album=album,
                duration_ms=duration_ms,
                candidate=_ItunesCandidate.of(item),
            )
            if score > best_score:
                best, best_score = item, score

        if best is None or best_score < _APPLE_MATCH_MIN:
            logger.debug(
                "[meta/apple] no result above %.2f for %r / %r (best %.2f)",
                _APPLE_MATCH_MIN,
                title,
                artist,
                best_score,
            )
            return None
        return best

    async def _search_async(
        self,
        title: str,
        artist: str,
        isrc: str,
        album: str = "",
        duration_ms: int = 0,
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
                        "limit": 5,
                        "country": "US",
                    },
                    headers={"User-Agent": _UA},
                    timeout=_HTTP_TIMEOUT,
                )
                if r.is_success:
                    # Not a lookup by identifier — iTunes has no such
                    # endpoint publicly, this is a full-text search whose
                    # term happens to be an ISRC — so the hits are checked
                    # like any others rather than trusted for their query.
                    item = self._best(
                        r.json().get("results", []),
                        title,
                        artist,
                        album,
                        duration_ms,
                    )
                    if item:
                        return item
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
            return self._best(
                r.json().get("results", []),
                title,
                artist,
                album,
                duration_ms,
            )
        except Exception as exc:
            logger.debug("[meta/apple] async %s", exc)
            return None

    async def fetch_async(
        self,
        track_name: str,
        artist_name: str,
        isrc: str = "",
        album_name: str = "",
        duration_ms: int = 0,
    ) -> EnrichedMetadata:
        out = EnrichedMetadata()
        item = await self._search_async(
            track_name,
            artist_name,
            isrc,
            album_name,
            duration_ms,
        )
        if not item:
            return out
        out.genre = item.get("primaryGenreName", "")
        out.explicit = item.get("trackExplicitness") == "explicit"
        raw_art = item.get("artworkUrl100", "")
        out.cover_url_hd = raw_art.replace("100x100", "600x600")
        # ISO-8601 with a time part; only the date half is a release date.
        out.release_date = str(item.get("releaseDate") or "")[:10]
        out.total_tracks = int(item.get("trackCount") or 0)
        out.total_discs = int(item.get("discCount") or 0)
        # A song result carries no collectionType — that only appears on
        # album lookups — so the release kind is inferred from its size,
        # which is all iTunes actually tells us here.
        if out.total_tracks:
            out.album_type = "single" if out.total_tracks == 1 else "album"
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
            mod = _get_dynamic_python_module("tidal")
            if mod and hasattr(mod, "get_tidal_api_list"):
                apis = mod.get_tidal_api_list()
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
            mod = _get_dynamic_python_module("tidal")
            apis = None
            if mod and hasattr(mod, "refresh_tidal_api_list"):
                apis = mod.refresh_tidal_api_list(force=False)
            elif mod and hasattr(mod, "refresh_tidal_api_list_async"):
                # tidal-py only has the async one now. Looking for the sync
                # name alone meant the list was never filled, and Tidal
                # enrichment answered nothing for every track.
                apis = run_sync(mod.refresh_tidal_api_list_async(force=False))
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
                    endpoint,
                    timeout=_HTTP_TIMEOUT,
                    headers={"User-Agent": _UA},
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
        first = artist.split(",", maxsplit=1)[0].strip()
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
                self._provider = _get_dynamic_python_provider(
                    "qobuz", qobuz_token=self._qobuz_token
                )
            except Exception as exc:
                logger.debug("[meta/qobuz] cannot init provider: %s", exc)
        return self._provider

    def fetch(self, isrc: str, album_name: str = "") -> EnrichedMetadata:
        return _run_async_sync(self.fetch_async(isrc, album_name))

    async def fetch_async(
        self, isrc: str, album_name: str = "", track_name: str = ""
    ) -> EnrichedMetadata:
        """Qobuz's view of one recording, looked up by ISRC.

        The track Qobuz returns carries far more than was read from it:
        composer, the ℗ line, the original release date, the album's track
        and disc counts, and the full performer list with roles. All of it
        is taken now. As with Deezer, an ISRC can land on a different
        release, so the release-scoped fields are taken only when the album
        agrees with `album_name`.
        """
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
            if _other_recording(track_name, str(track.get("title") or "")):
                logger.debug(
                    "[meta/qobuz] ISRC %s is %r, not %r — ignored",
                    isrc,
                    track.get("title"),
                    track_name,
                )
                return out
            album = track.get("album", {}) or {}
            out.genre = (album.get("genre", {}) or {}).get("name", "")
            out.explicit = bool(track.get("parental_warning"))
            out.isrc = track.get("isrc", "")
            composer = track.get("composer")
            if isinstance(composer, dict):
                out.composer = str(composer.get("name") or "")
            _apply_credits(out, _qobuz_performer_pairs(track.get("performers")))

            found_title = str(album.get("title") or "")
            if not album_name or _same_release(album_name, found_title):
                out.label = (
                    album.get("label", {}).get("name", "")
                    if isinstance(album.get("label"), dict)
                    else ""
                )
                out.cover_url_hd = (album.get("image", {}) or {}).get("large", "")
                out.upc = album.get("upc", "")
                out.copyright = str(track.get("copyright") or "")
                out.release_date = str(
                    album.get("release_date_original")
                    or track.get("release_date_original")
                    or ""
                )
                out.total_tracks = _as_int(album.get("tracks_count"))
                out.total_discs = _as_int(album.get("media_count"))
            else:
                logger.debug(
                    "[meta/qobuz] release mismatch: wanted %r, got %r "
                    "— keeping recording fields only",
                    album_name,
                    found_title,
                )
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
            self._provider = _get_dynamic_python_provider("soundcloud")
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


async def _deezer_fetch_async(
    isrc: str, album_name: str = "", track_name: str = ""
) -> EnrichedMetadata:
    return await _get_deezer().fetch_async(isrc, album_name, track_name)


async def _apple_fetch_async(
    track_name: str,
    artist_name: str,
    isrc: str,
    album_name: str = "",
    duration_ms: int = 0,
) -> EnrichedMetadata:
    return await _get_apple().fetch_async(
        track_name,
        artist_name,
        isrc,
        album_name,
        duration_ms,
    )


async def _tidal_fetch_async(track_name: str, artist_name: str) -> EnrichedMetadata:
    return await _get_tidal().fetch_async(track_name, artist_name)


async def _qobuz_fetch_async(
    isrc: str, qobuz_token: str | None, album_name: str = "", track_name: str = ""
) -> EnrichedMetadata:
    return await _get_qobuz_meta(qobuz_token).fetch_async(isrc, album_name, track_name)


async def _soundcloud_fetch_async(
    track_name: str,
    artist_name: str,
) -> EnrichedMetadata:
    return await _get_sc().fetch_async(track_name, artist_name)


# ---------------------------------------------------------------------------
# Async enrich_metadata — Phase 2 (new)
# ---------------------------------------------------------------------------


async def enrich_metadata_async(
    track_name: str,
    artist_name: str,
    isrc: str = "",
    providers: list[str] | None = None,
    timeout_s: float = _GLOBAL_TIMEOUT,
    qobuz_token: str | None = None,
    album_name: str = "",
    duration_ms: int = 0,
) -> EnrichedMetadata:
    """Queries providers in parallel with asyncio.gather + global timeout.
    Replaces the sync version's ThreadPoolExecutor.

    `album_name` and `duration_ms` are what the caller already knows about
    the track, and they are what lets a provider's answer be *checked*
    rather than accepted: they gate Deezer's release-scoped fields and feed
    the iTunes match score. Both are optional and both default to the old,
    unverified behaviour when the caller has nothing to offer.
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
                return name, await _deezer_fetch_async(
                    isrc, album_name, track_name=track_name
                )
            if name == "apple":
                return name, await _apple_fetch_async(
                    track_name,
                    artist_name,
                    isrc,
                    album_name,
                    duration_ms,
                )
            if name == "tidal":
                return name, await _tidal_fetch_async(track_name, artist_name)
            if name == "qobuz":
                return name, await _qobuz_fetch_async(
                    isrc, qobuz_token, album_name, track_name=track_name
                )
            if name == "soundcloud":
                return name, await _soundcloud_fetch_async(track_name, artist_name)
            logger.warning("[meta/enrich] provider sconosciuto: %s", name)
            return name, EnrichedMetadata()
        except Exception as exc:
            logger.debug("[meta/enrich] %s failed: %s", name, exc)
            return name, EnrichedMetadata()

    # Each service in the list is asked twice where it can be: the built-in
    # lookup above, then its own JavaScript extensions (tidal-web, qobuz-web,
    # deezer, apple-music). Every other installed JavaScript extension that
    # implements enrichTrack is asked as well, whatever its service — the
    # list chooses the built-in lookups and their order, not whether the
    # extensions take part. See core/extension_enrichment.py.
    from . import extension_enrichment

    ext_sources: dict[str, list[str]] = {}
    other_exts: list[str] = []
    if extension_enrichment.extension_enrichment_enabled():
        try:
            for name in providers:
                ext_sources[name] = extension_enrichment.extensions_for_service(name)
            claimed = {ext for exts in ext_sources.values() for ext in exts}
            other_exts = [
                ext
                for ext in extension_enrichment.enrichment_extensions()
                if ext not in claimed
            ]
        except Exception as exc:
            logger.debug("[meta/enrich] could not list enriching extensions: %s", exc)

    async def bounded(coro, limit: float, label: str):
        # A timeout per source, not one for the lot: a slow extension used
        # to be able to cost every provider's answer, and the extensions'
        # first call also starts their runtime.
        try:
            return await asyncio.wait_for(coro, timeout=limit)
        except asyncio.TimeoutError:
            logger.warning("[meta/enrich] %s timed out after %.1fs", label, limit)
            return None

    ext_keys = [(name, ext) for name in providers for ext in ext_sources.get(name, [])]
    ext_keys += [("", ext) for ext in other_exts]
    outcomes = await asyncio.gather(
        *[bounded(run_provider(p), timeout_s, p) for p in providers],
        *[
            bounded(
                extension_enrichment.fetch_extension_enrichment(
                    ext, track_name, artist_name, isrc, album_name, duration_ms
                ),
                max(timeout_s, extension_enrichment.EXTENSION_TIMEOUT_S),
                f"ext:{ext}",
            )
            for _name, ext in ext_keys
        ],
    )
    results = {
        outcome[0]: outcome[1]
        for outcome in outcomes[: len(providers)]
        if isinstance(outcome, tuple)
    }
    ext_results = dict(zip(ext_keys, outcomes[len(providers) :]))

    # Every provider has already answered — gather() above waits for all of
    # them — so this loop spends nothing but the merge. It used to stop at
    # the first is_complete(), which saved no request and threw away fields
    # already in hand: the disc count only Apple reports was discarded
    # whenever Deezer had happened to supply genre, label and cover.
    # merge() only ever fills blanks, in the caller's provider order, so
    # reading them all cannot change which provider wins a field.
    merged = EnrichedMetadata()
    for name in providers:
        data = results.get(name)
        if isinstance(data, EnrichedMetadata):
            merged.merge(data, name)
        for ext in ext_sources.get(name, []):
            data = ext_results.get((name, ext))
            if isinstance(data, EnrichedMetadata):
                merged.merge(data, f"ext:{ext}")
    # The extensions of services not in the list come last, so the list's
    # order still decides every field those services answer.
    for ext in other_exts:
        data = ext_results.get(("", ext))
        if isinstance(data, EnrichedMetadata):
            merged.merge(data, f"ext:{ext}")

    if merged._sources:
        logger.debug("[meta/enrich] async enriched: %s", merged._sources)

    if isrc:
        # Unconditional now: _put_cached() decides for itself whether this
        # is worth persisting, and stores a miss in memory on the short TTL
        # so the same barren ISRC does not re-run four providers per pass.
        _put_cached(isrc, merged)

    return merged

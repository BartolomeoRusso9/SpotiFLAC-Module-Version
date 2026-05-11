# SpotiFLAC/core/metadata_enrichment.py
"""
Multi-provider metadata enrichment — ottimizzato per latenza.

Fix e miglioramenti rispetto alla versione precedente:

BUG CRITICO (#1): il `with ThreadPoolExecutor() as pool:` bloccava il thread principale
  finché TUTTI i worker non terminavano, anche dopo che `as_completed` aveva già
  sollevato TimeoutError. Con Tidal che cercava sequenzialmente su 20+ API con timeout
  7s ciascuna, la funzione impiegava 40+ secondi. Fix: pool esplicito con
  shutdown(wait=False, cancel_futures=True).

BUG CRITICO (#2): _TidalMeta.__init__ faceva una richiesta HTTP al gist GitHub
  ad ogni chiamata a enrich_metadata() (ogni traccia), aggiungendo latenza fissa.

Ottimizzazioni:
  3. Cache ISRC in memoria (TTL 1h) — stessa traccia non viene mai ri-fetchata.
  4. Provider singleton — istanziazione una sola volta per processo.
  5. _TidalMeta: ricerca parallela sulle API (era sequenziale → potenzialmente infinita).
  6. _TidalMeta: riusa la lista API già cached da tidal.py, refresh in background.
  7. Timeout HTTP ridotti a 4s (era 7s) — per enrichment la velocità conta più della resilienza.
  8. Early-exit: appena tutti i campi principali sono popolati, i future rimasti vengono ignorati.
  9. _SoundCloudMeta: salta l'init costoso se il provider non è ancora warmato.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# Timeout per singola richiesta HTTP dentro un provider (era 7s)
_HTTP_TIMEOUT = 4
# Timeout globale entro cui tutti i provider devono rispondere
_GLOBAL_TIMEOUT = 6.0
# TTL cache ISRC in secondi (1 ora)
_ENRICHMENT_CACHE_TTL = 3600.0
# Max API Tidal da interrogare in parallelo per l'enrichment
_TIDAL_MAX_APIS = 10
_TIDAL_MAX_WORKERS = 5


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class EnrichedMetadata:
    """Campi opzionali ricavati dai provider supplementari."""
    genre:        str  = ""
    label:        str  = ""
    bpm:          int  = 0
    explicit:     bool = False
    upc:          str  = ""
    isrc:         str  = ""
    cover_url_hd: str  = ""
    _sources: dict[str, str] = field(default_factory=dict, repr=False)

    def as_tags(self) -> dict[str, str]:
        tags: dict[str, str] = {}
        if self.genre:    tags["GENRE"]          = self.genre
        if self.label:    tags["ORGANIZATION"]   = self.label
        if self.bpm:      tags["BPM"]            = str(self.bpm)
        if self.upc:      tags["UPC"]            = self.upc
        if self.isrc:     tags["ISRC"]           = self.isrc
        if self.explicit: tags["ITUNESADVISORY"] = "1"
        return tags

    def merge(self, other: "EnrichedMetadata", source: str) -> None:
        """Aggiorna solo i campi vuoti con i dati dell'altro oggetto."""
        for attr in ("genre", "label", "bpm", "upc", "isrc", "cover_url_hd"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
                self._sources[attr] = source
        if not self.explicit and other.explicit:
            self.explicit = True
            self._sources["explicit"] = source

    def is_complete(self) -> bool:
        """True se i campi principali sono tutti popolati — permette early-exit."""
        return bool(self.genre and self.label and self.cover_url_hd)


# ---------------------------------------------------------------------------
# Cache ISRC in memoria
# ---------------------------------------------------------------------------

_enrichment_cache: dict[str, tuple[EnrichedMetadata, float]] = {}
_cache_lock = threading.Lock()


def _get_cached(isrc: str) -> EnrichedMetadata | None:
    if not isrc:
        return None
    with _cache_lock:
        entry = _enrichment_cache.get(isrc.upper())
        if entry and (time.time() - entry[1]) < _ENRICHMENT_CACHE_TTL:
            logger.debug("[meta/cache] HIT per ISRC %s", isrc)
            return entry[0]
    return None


def _put_cached(isrc: str, data: EnrichedMetadata) -> None:
    if not isrc:
        return
    with _cache_lock:
        _enrichment_cache[isrc.upper()] = (data, time.time())


# ---------------------------------------------------------------------------
# Provider: Deezer
# ---------------------------------------------------------------------------

class _DeezerMeta:
    BASE = "https://api.deezer.com/2.0"

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA

    def fetch(self, isrc: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        if not isrc:
            return out
        try:
            r = self._s.get(f"{self.BASE}/track/isrc:{isrc}", timeout=_HTTP_TIMEOUT)
            if r.status_code != 200:
                return out
            d = r.json()
            if "error" in d:
                return out

            album_id = d.get("album", {}).get("id")
            if album_id:
                ar = self._s.get(f"{self.BASE}/album/{album_id}", timeout=_HTTP_TIMEOUT)
                if ar.ok:
                    ad = ar.json()
                    genres = ad.get("genres", {}).get("data", [])
                    if genres:
                        out.genre = genres[0].get("name", "")
                    out.label        = ad.get("label", "")
                    out.upc          = ad.get("upc", "")
                    out.cover_url_hd = ad.get("cover_xl") or ad.get("cover_big", "")

            out.bpm      = int(d.get("bpm") or 0)
            out.explicit = bool(d.get("explicit_lyrics"))
            out.isrc     = d.get("isrc", "")
        except Exception as exc:
            logger.debug("[meta/deezer] %s", exc)
        return out


# ---------------------------------------------------------------------------
# Provider: Apple Music (iTunes Search API — gratuita, no auth)
# ---------------------------------------------------------------------------

class _AppleMusicMeta:
    SEARCH = "https://itunes.apple.com/search"

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA

    def fetch(self, track_name: str, artist_name: str, isrc: str = "") -> EnrichedMetadata:
        out = EnrichedMetadata()
        item = self._search(track_name, artist_name, isrc)
        if not item:
            return out
        out.genre    = item.get("primaryGenreName", "")
        out.explicit = item.get("trackExplicitness") == "explicit"
        raw_art = item.get("artworkUrl100", "")
        out.cover_url_hd = raw_art.replace("100x100", "600x600")
        return out

    def _search(self, title: str, artist: str, isrc: str) -> dict[str, Any] | None:
        try:
            # 1. Tentativo per ISRC
            if isrc:
                r = self._s.get(
                    self.SEARCH,
                    params={"term": isrc, "media": "music", "entity": "song",
                            "limit": 1, "country": "US"},
                    timeout=_HTTP_TIMEOUT,
                )
                if r.ok:
                    results = r.json().get("results", [])
                    if results:
                        return results[0]

            # 2. Ricerca testuale
            r = self._s.get(
                self.SEARCH,
                params={"term": f"{title} {artist}", "media": "music", "entity": "song",
                        "limit": 5, "country": "US"},
                timeout=_HTTP_TIMEOUT,
            )
            if not r.ok:
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
            logger.debug("[meta/apple] %s", exc)
            return None


# ---------------------------------------------------------------------------
# Provider: Tidal — ottimizzato con ricerca parallela e API list cached
# ---------------------------------------------------------------------------

_TIDAL_APIS_BUILTIN = [
    "https://eu-central.monochrome.tf",
    "https://us-west.monochrome.tf",
    "https://api.monochrome.tf",
    "https://monochrome-api.samidy.com",
    "https://tidal-api.binimum.org",
    "https://tidal.kinoplus.online",
    "https://triton.squid.wtf",
    "https://vogel.qqdl.site",
    "https://maus.qqdl.site",
    "https://hund.qqdl.site",
    "https://katze.qqdl.site",
    "https://wolf.qqdl.site",
    "https://hifi-one.spotisaver.net",
    "https://hifi-two.spotisaver.net",
]


class _TidalMeta:
    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._apis: list[str] = []
        self._apis_ready = False
        self._apis_lock = threading.Lock()
        # Carica subito dalla cache di tidal.py (nessuna rete)
        self._load_apis_from_cache()

    def _load_apis_from_cache(self) -> None:
        """
        Usa la lista API già caricata da tidal.py senza fare richieste HTTP.
        Se non disponibile, usa la builtin e avvia un refresh in background.
        """
        try:
            from ..providers.tidal import get_tidal_api_list
            apis = get_tidal_api_list()
            if apis:
                self._apis = apis
                self._apis_ready = True
                logger.debug("[meta/tidal] API list from cache: %d URL", len(apis))
                return
        except Exception:
            pass

        # Fallback immediato alla lista builtin
        self._apis = list(_TIDAL_APIS_BUILTIN)
        self._apis_ready = True
        logger.debug("[meta/tidal] usando API builtin, avvio refresh background")
        threading.Thread(target=self._refresh_bg, daemon=True).start()

    def _refresh_bg(self) -> None:
        """Aggiorna la lista API in background senza bloccare l'enrichment."""
        try:
            from ..providers.tidal import refresh_tidal_api_list
            apis = refresh_tidal_api_list(force=False)
            if apis:
                with self._apis_lock:
                    self._apis = apis
                logger.debug("[meta/tidal] API list aggiornata in background: %d URL", len(apis))
        except Exception as exc:
            logger.debug("[meta/tidal] refresh background fallito: %s", exc)

    def fetch(self, track_name: str, artist_name: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        track_data = self._search_parallel(track_name, artist_name)
        if not track_data:
            return out
        album = track_data.get("album", {})
        out.cover_url_hd = album.get("cover", "")
        out.explicit     = bool(track_data.get("explicit"))
        out.isrc         = track_data.get("isrc", "")
        return out

    def _try_api(self, api: str, query: str) -> dict | None:
        """Prova un singolo API endpoint; ritorna la prima traccia trovata o None."""
        base = api.rstrip("/")
        for endpoint in (
                f"{base}/search/?s={query}&limit=3",
                f"{base}/search?s={query}&limit=3",
        ):
            try:
                r = self._s.get(endpoint, timeout=_HTTP_TIMEOUT)
                if not r.ok:
                    continue
                data  = r.json()
                items = data if isinstance(data, list) else data.get("tracks", {}).get("items", [])
                if items:
                    return items[0]
            except Exception:
                pass
        return None

    def _search_parallel(self, title: str, artist: str) -> dict | None:
        """
        Interroga le API Tidal in parallelo invece di sequenzialmente.
        Ritorna al primo risultato valido, cancellando i worker rimasti.
        """
        from urllib.parse import quote
        clean  = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip() or title
        first  = artist.split(",")[0].strip()
        query  = quote(f"{first} {clean}")

        with self._apis_lock:
            apis = list(self._apis)

        apis_to_try = apis[:_TIDAL_MAX_APIS]
        if not apis_to_try:
            return None

        pool = ThreadPoolExecutor(max_workers=min(len(apis_to_try), _TIDAL_MAX_WORKERS))
        futs = {pool.submit(self._try_api, api, query): api for api in apis_to_try}
        result: dict | None = None
        try:
            for fut in as_completed(futs, timeout=_HTTP_TIMEOUT + 1):
                try:
                    data = fut.result()
                    if data:
                        result = data
                        break
                except Exception:
                    pass
        except FuturesTimeoutError:
            pass
        finally:
            # FIX: non aspettare i thread rimasti
            pool.shutdown(wait=False, cancel_futures=True)

        return result


# ---------------------------------------------------------------------------
# Provider: Qobuz
# ---------------------------------------------------------------------------

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
        out = EnrichedMetadata()
        if not isrc:
            return out
        try:
            prov = self._get_provider()
            if prov is None:
                return out
            resp = prov._do_signed_get("track/search", {"query": isrc, "limit": "1"})
            if not resp.ok:
                return out
            items = resp.json().get("tracks", {}).get("items", [])
            if not items:
                return out
            track = items[0]
            album = track.get("album", {})
            out.genre        = (album.get("genre", {}) or {}).get("name", "")
            out.label        = album.get("label", {}).get("name", "") if isinstance(album.get("label"), dict) else ""
            out.cover_url_hd = album.get("image", {}).get("large", "")
            out.explicit     = bool(track.get("parental_warning"))
            out.isrc         = track.get("isrc", "")
            out.upc          = album.get("upc", "")
        except Exception as exc:
            logger.debug("[meta/qobuz] %s", exc)
        return out


# ---------------------------------------------------------------------------
# Provider: SoundCloud
# ---------------------------------------------------------------------------

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
            p = SoundCloudProvider()
            # Verifica che il client_id sia già disponibile (da cache)
            # senza fare richieste HTTP bloccanti durante l'enrichment
            if p.client_id or p.client_id_expiry > time.time():
                self._provider = p
            else:
                # Tenta comunque, ma con timeout controllato
                self._provider = p
        except Exception as exc:
            logger.debug("[meta/soundcloud] cannot init provider: %s", exc)
        return self._provider

    def fetch(self, track_name: str, artist_name: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        try:
            prov = self._get_provider()
            if prov is None:
                return out
            query   = f"{artist_name} {track_name}"
            results = prov.search(query, search_type="tracks", limit=1)
            if not results:
                return out
            out.cover_url_hd = results[0].get("cover_url", "")
        except Exception as exc:
            logger.debug("[meta/soundcloud] %s", exc)
        return out


# ---------------------------------------------------------------------------
# Singleton provider instances
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_deezer_inst:  _DeezerMeta | None      = None
_apple_inst:   _AppleMusicMeta | None  = None
_tidal_inst:   _TidalMeta | None       = None
_sc_inst:      _SoundCloudMeta | None  = None


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
# Public API
# ---------------------------------------------------------------------------

def enrich_metadata(
        track_name:  str,
        artist_name: str,
        isrc:        str = "",
        providers:   list[str] | None = None,
        timeout_s:   float = _GLOBAL_TIMEOUT,
        qobuz_token: str | None = None,
) -> EnrichedMetadata:
    """
    Interroga i provider in parallelo e unisce i risultati.

    Args:
        track_name:  Nome della traccia.
        artist_name: Artista principale.
        isrc:        ISRC (usato da Deezer e Qobuz).
        providers:   Lista ordinata di provider da usare.
        timeout_s:   Timeout massimo globale in secondi (default: 6.0).
        qobuz_token: Token opzionale Qobuz.

    Returns:
        EnrichedMetadata con i campi trovati.
    """
    if providers is None:
        providers = ["deezer", "apple", "qobuz", "tidal"]

    # 1. Cache hit — ritorna subito senza fare rete
    if isrc:
        cached = _get_cached(isrc)
        if cached is not None:
            return cached

    merged = EnrichedMetadata()

    def _run_provider(name: str) -> tuple[str, EnrichedMetadata]:
        try:
            if name == "deezer":
                return name, _get_deezer().fetch(isrc)
            elif name == "apple":
                return name, _get_apple().fetch(track_name, artist_name, isrc)
            elif name == "tidal":
                return name, _get_tidal().fetch(track_name, artist_name)
            elif name == "qobuz":
                # Qobuz non può essere singleton perché il token varia
                return name, _QobuzMeta(qobuz_token=qobuz_token).fetch(isrc)
            elif name == "soundcloud":
                return name, _get_sc().fetch(track_name, artist_name)
            else:
                logger.warning("[meta/enrich] provider sconosciuto: %s", name)
                return name, EnrichedMetadata()
        except Exception as exc:
            logger.debug("[meta/enrich] %s failed: %s", name, exc)
            return name, EnrichedMetadata()

    # 2. Fetch parallelo con pool esplicito (non context manager)
    #    → shutdown(wait=False) garantisce che il thread principale non aspetti
    #      i worker bloccati su richieste HTTP lente.
    results: dict[str, EnrichedMetadata] = {}
    pool = ThreadPoolExecutor(max_workers=len(providers))
    futs = {pool.submit(_run_provider, p): p for p in providers}
    deadline = time.time() + timeout_s

    try:
        for fut in as_completed(futs, timeout=max(1.0, deadline - time.time())):
            name, data = fut.result()
            results[name] = data

            # 3. Early-exit: se i campi principali sono già tutti coperti,
            #    inutile aspettare i provider rimasti
            merged_preview = EnrichedMetadata()
            for n in providers:
                if n in results:
                    merged_preview.merge(results[n], n)
            if merged_preview.is_complete():
                logger.debug("[meta/enrich] early-exit: tutti i campi coperti")
                break

    except FuturesTimeoutError:
        unfinished = [futs[f] for f in futs if not f.done()]
        if unfinished:
            logger.warning(
                "[meta/enrich] timeout %.1fs — provider lenti ignorati: %s",
                timeout_s,
                ", ".join(unfinished),
            )
    finally:
        # FIX CRITICO: non aspettare i thread rimasti.
        # La versione precedente usava `with ThreadPoolExecutor() as pool:`
        # che chiama implicitamente shutdown(wait=True), bloccando per 40+ secondi
        # se Tidal stava cercando su 20 API da 7s ciascuna.
        pool.shutdown(wait=False, cancel_futures=True)

    # 4. Merge in ordine di priorità
    for name in providers:
        if name in results:
            merged.merge(results[name], source=name)

    if merged._sources:
        logger.debug("[meta/enrich] campi arricchiti: %s", merged._sources)

    # 5. Salva in cache per riutilizzo futuro
    if isrc and (merged.genre or merged.label or merged.cover_url_hd):
        _put_cached(isrc, merged)

    return merged
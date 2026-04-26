# SpotiFLAC/core/metadata_enrichment.py
"""
Multi-provider metadata enrichment.

Arricchisce TrackMetadata con dati aggiuntivi (label, BPM, genere, cover HD, UPC…)
da Tidal, Qobuz, Deezer e Apple Music.
I provider vengono interrogati in parallelo; i risultati vengono uniti con priorità
configurabile (chi appare prima nella lista "wins" per ogni campo).

Uso minimo (nel tagger/downloader):
    from .metadata_enrichment import enrich_metadata
    extra = enrich_metadata(metadata, providers=["deezer", "apple", "tidal", "qobuz"])
    embed_metadata(..., extra_tags=extra.as_tags())
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Result container                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class EnrichedMetadata:
    """Campi opzionali ricavati dai provider supplementari."""
    genre:       str = ""
    label:       str = ""
    bpm:         int = 0
    explicit:    bool = False
    upc:         str = ""
    isrc:        str = ""
    # Cover URL ad alta risoluzione
    cover_url_hd: str = ""
    # Da quale provider viene ogni campo (debug)
    _sources: dict[str, str] = field(default_factory=dict, repr=False)

    def as_tags(self) -> dict[str, str]:
        """Ritorna un dict compatibile con embed_metadata(extra_tags=…)."""
        tags: dict[str, str] = {}
        if self.genre:   tags["GENRE"]        = self.genre
        if self.label:   tags["ORGANIZATION"] = self.label
        if self.bpm:     tags["BPM"]          = str(self.bpm)
        if self.upc:     tags["UPC"]          = self.upc
        if self.isrc:    tags["ISRC"]         = self.isrc
        if self.explicit:tags["ITUNESADVISORY"] = "1"
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


# --------------------------------------------------------------------------- #
# Provider: Deezer                                                             #
# --------------------------------------------------------------------------- #

class _DeezerMeta:
    """Lookup per ISRC via API pubblica Deezer."""

    BASE = "https://api.deezer.com/2.0"

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA

    def fetch(self, isrc: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        if not isrc:
            return out
        try:
            r = self._s.get(f"{self.BASE}/track/isrc:{isrc}", timeout=12)
            if r.status_code != 200:
                return out
            d = r.json()
            if "error" in d:
                return out

            # Genere dall'album
            album_id = d.get("album", {}).get("id")
            if album_id:
                ar = self._s.get(f"{self.BASE}/album/{album_id}", timeout=10)
                if ar.ok:
                    ad = ar.json()
                    genres = ad.get("genres", {}).get("data", [])
                    if genres:
                        out.genre = genres[0].get("name", "")
                    out.label   = ad.get("label", "")
                    out.upc     = ad.get("upc", "")
                    # Cover 1000x1000
                    out.cover_url_hd = ad.get("cover_xl") or ad.get("cover_big", "")

            out.bpm      = int(d.get("bpm") or 0)
            out.explicit = bool(d.get("explicit_lyrics"))
            out.isrc     = d.get("isrc", "")
        except Exception as exc:
            logger.debug("[meta/deezer] %s", exc)
        return out


# --------------------------------------------------------------------------- #
# Provider: Apple Music (iTunes Search API — free, no auth)                   #
# --------------------------------------------------------------------------- #

class _AppleMusicMeta:
    """
    Usa iTunes Search API (gratuita).
    Ritorna genere, label, esplicit e URL cover 600x600.
    """

    SEARCH = "https://itunes.apple.com/search"
    LOOKUP = "https://itunes.apple.com/lookup"

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA

    def fetch(self, track_name: str, artist_name: str, isrc: str = "") -> EnrichedMetadata:
        out = EnrichedMetadata()
        item = self._search(track_name, artist_name)
        if not item:
            return out
        out.genre    = item.get("primaryGenreName", "")
        out.explicit = item.get("trackExplicitness") == "explicit"
        # Cover 600×600: rimpiazza "100x100" con "600x600"
        raw_art = item.get("artworkUrl100", "")
        out.cover_url_hd = raw_art.replace("100x100", "600x600")
        # Collection (album) info → label non disponibile via iTunes pubblica
        return out

    def _search(self, title: str, artist: str) -> dict[str, Any] | None:
        try:
            r = self._s.get(
                self.SEARCH,
                params={
                    "term":    f"{title} {artist}",
                    "media":   "music",
                    "entity":  "song",
                    "limit":   5,
                    "country": "US",
                },
                timeout=12,
            )
            if not r.ok:
                return None
            results = r.json().get("results", [])
            if not results:
                return None
            # Scegli il risultato con l'artista più simile
            artist_lc = artist.lower()
            for item in results:
                if artist_lc in item.get("artistName", "").lower():
                    return item
            return results[0]
        except Exception as exc:
            logger.debug("[meta/apple] %s", exc)
            return None


# --------------------------------------------------------------------------- #
# Provider: Tidal (via API mirror — stesso sistema di tidal.py)               #
# --------------------------------------------------------------------------- #

class _TidalMeta:
    """
    Recupera metadati extra da Tidal tramite le API mirror già usate
    per il download (ricerca per track_name/artist).
    """

    _APIS = [
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

    _GIST_URL = "https://gist.githubusercontent.com/afkarxyz/2ce772b943321b9448b454f39403ce25/raw"

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._merged_apis = list(self._APIS)
        self._fetch_and_merge_apis()

    def _fetch_and_merge_apis(self) -> None:
        """Scarica le API dal gist e le unisce a quelle di base senza duplicati."""
        try:
            r = self._s.get(self._GIST_URL, timeout=8)
            if r.ok:
                gist_urls = r.json()
                if isinstance(gist_urls, list):
                    for url in gist_urls:
                        clean_url = url.strip().rstrip("/")
                        if clean_url and clean_url not in self._merged_apis:
                            self._merged_apis.append(clean_url)
            logger.debug("[meta/tidal] Total APIs loaded: %d", len(self._merged_apis))
        except Exception as exc:
            logger.debug("[meta/tidal] Failed to fetch gist APIs: %s", exc)

    def fetch(self, track_name: str, artist_name: str) -> EnrichedMetadata:
        out = EnrichedMetadata()
        track_data = self._search_track(track_name, artist_name)
        if not track_data:
            return out
        album = track_data.get("album", {})
        out.cover_url_hd = album.get("cover", "")
        out.explicit     = bool(track_data.get("explicit"))
        out.isrc         = track_data.get("isrc", "")
        return out

    def _search_track(self, title: str, artist: str) -> dict | None:
        from urllib.parse import quote
        q = quote(f"{artist} {title}")
        for api in self._merged_apis:
            for endpoint in (
                    f"{api.rstrip('/')}/search/?s={q}&limit=5",
                    f"{api.rstrip('/')}/search?s={q}&limit=5",
            ):
                try:
                    r = self._s.get(endpoint, timeout=8)
                    if not r.ok:
                        continue
                    data = r.json()
                    items = data if isinstance(data, list) else data.get("tracks", {}).get("items", [])
                    if items:
                        return items[0]
                except Exception:
                    continue
        return None


# --------------------------------------------------------------------------- #
# Provider: Qobuz (usa l'API firmata già in qobuz.py)                         #
# --------------------------------------------------------------------------- #

class _QobuzMeta:
    """
    Recupera metadati Qobuz tramite ISRC (signed API).
    Richiede le stesse credenziali usate per il download.
    """

    def __init__(self, qobuz_token: str | None = None) -> None:
        self._provider: Any = None
        self._qobuz_token = qobuz_token  # ← aggiunto

    def _get_provider(self) -> Any:
        if self._provider is None:
            try:
                from ..providers.qobuz import QobuzProvider
                self._provider = QobuzProvider(qobuz_token=self._qobuz_token)  # ← aggiunto
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


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

_PROVIDERS = {
    "deezer": _DeezerMeta,
    "apple":  _AppleMusicMeta,
    "tidal":  _TidalMeta,
    "qobuz":  _QobuzMeta,
}


def enrich_metadata(
        track_name:  str,
        artist_name: str,
        isrc:        str = "",
        providers:   list[str] | None = None,
        timeout_s:   float = 15.0,
        qobuz_token: str | None = None,
) -> EnrichedMetadata:
    """
    Interroga i provider in parallelo e unisce i risultati.

    Args:
        track_name:  Nome della traccia.
        artist_name: Artista principale.
        isrc:        ISRC (usato da Deezer e Qobuz).
        providers:   Lista ordinata di provider da usare.
                     Default: ["deezer", "apple", "qobuz", "tidal"]
        timeout_s:   Timeout massimo globale (secondi).

    Returns:
        EnrichedMetadata con i campi trovati (quelli non trovati restano "").
    """
    if providers is None:
        providers = ["deezer", "apple", "qobuz", "tidal"]

    merged = EnrichedMetadata()

    def _run_provider(name: str) -> tuple[str, EnrichedMetadata]:
        cls = _PROVIDERS.get(name)
        if cls is None:
            return name, EnrichedMetadata()
        try:
            # ← istanzia _QobuzMeta con il token, gli altri invariati
            if name == "qobuz":
                inst = cls(qobuz_token=qobuz_token)
            else:
                inst = cls()

            if name == "deezer":
                return name, inst.fetch(isrc)
            elif name == "apple":
                return name, inst.fetch(track_name, artist_name, isrc)
            elif name == "tidal":
                return name, inst.fetch(track_name, artist_name)
            elif name == "qobuz":
                return name, inst.fetch(isrc)
        except Exception as exc:
            logger.debug("[meta/enrich] %s failed: %s", name, exc)
        return name, EnrichedMetadata()

    # Fetch parallelo — il primo risultato per ogni campo vince (ordine lista)
    results: dict[str, EnrichedMetadata] = {}
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futs = {pool.submit(_run_provider, p): p for p in providers}
        deadline = time.time() + timeout_s
        try:
            for fut in as_completed(futs, timeout=max(1.0, deadline - time.time())):
                name, data = fut.result()
                results[name] = data
        except TimeoutError:
            # Trova quali provider non hanno finito in tempo
            unfinished = [futs[fut] for fut in futs if not fut.done()]
            logger.warning("[meta/enrich] Timeout! Provider lenti ignorati: %s", ", ".join(unfinished))

    # Merge in ordine di priorità
    for name in providers:
        if name in results:
            merged.merge(results[name], source=name)

    if merged._sources:
        logger.debug("[meta/enrich] enriched fields: %s", merged._sources)

    return merged
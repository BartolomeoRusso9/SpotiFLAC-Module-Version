"""
TidalProvider — migliorato rispetto all'implementazione Go di riferimento.

Principali miglioramenti vs versione originale:
1. Fetch parallelo degli URL di download (ThreadPoolExecutor, prima risposta vince)
2. Retry con backoff esponenziale per ogni singola API (allineato a fetchTidalURLWithRetry Go)
3. Rilevamento risposta v2 (manifest) e v1 (OriginalTrackUrl) nel worker parallelo
4. Fallback qualità HI_RES → LOSSLESS invariato ma con errore tipato
5. Tutti i pattern go (remember_tidal_api_usage, round-robin) mantenuti
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from ..core.tagger import _print_mb_summary
from ..core.link_resolver import LinkResolver
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import requests

from ..core.errors import (
    TrackNotFoundError, ParseError,
    SpotiflacError, ErrorKind,
)
from ..core.http import HttpClient, RetryConfig
from ..core.models import TrackMetadata, DownloadResult
from ..core.musicbrainz import AsyncMBFetch
from ..core.tagger import embed_metadata
from .base import BaseProvider
from ..core.console import (
    print_source_banner, print_api_failure, print_quality_fallback,
)
from ..core.download_validation import validate_downloaded_track

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIDAL_APIS = [
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

_TIDAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_TIDAL_API_GIST_URL   = "https://gist.githubusercontent.com/afkarxyz/2ce772b943321b9448b454f39403ce25/raw"
_TIDAL_API_CACHE_FILE = "tidal-api-urls.json"

# Timeout e retry allineati al Go (tidalAPITimeoutMobile, tidalMaxRetries)
_API_TIMEOUT_S = 8
_MAX_RETRIES   = 1
_RETRY_DELAY_S = 0.3

# ---------------------------------------------------------------------------
# API list manager (invariato rispetto alla versione originale)
# ---------------------------------------------------------------------------

_tidal_api_list_mu:    threading.Lock = threading.Lock()
_tidal_api_list_state: dict | None    = None


def _get_cache_path() -> Path:
    cache_dir = Path.home() / ".cache" / "spotiflac"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _TIDAL_API_CACHE_FILE


def _clone_state(state: dict) -> dict:
    return {
        "urls":          list(state.get("urls", [])),
        "last_used_url": state.get("last_used_url", ""),
        "updated_at":    state.get("updated_at", 0),
        "source":        state.get("source", ""),
    }


def _normalize_tidal_api_urls(urls: list[str]) -> list[str]:
    seen:       set[str]  = set()
    normalized: list[str] = []
    for raw in urls:
        url = raw.strip().rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


def _load_tidal_api_list_state_locked() -> dict:
    global _tidal_api_list_state
    if _tidal_api_list_state is not None:
        return _clone_state(_tidal_api_list_state)

    cache_path = _get_cache_path()
    if not cache_path.exists():
        empty = {"urls": [], "last_used_url": "", "updated_at": 0, "source": ""}
        _tidal_api_list_state = _clone_state(empty)
        return _clone_state(empty)

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state["urls"] = _normalize_tidal_api_urls(state.get("urls", []))
        _tidal_api_list_state = _clone_state(state)
        return _clone_state(state)
    except Exception as exc:
        logger.warning("[tidal] failed to read API list cache: %s", exc)
        empty = {"urls": [], "last_used_url": "", "updated_at": 0, "source": ""}
        _tidal_api_list_state = _clone_state(empty)
        return _clone_state(empty)


def _save_tidal_api_list_state_locked(state: dict) -> None:
    global _tidal_api_list_state
    cache_path = _get_cache_path()
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        _tidal_api_list_state = _clone_state(state)
    except Exception as exc:
        logger.warning("[tidal] failed to write API list cache: %s", exc)


def _fetch_tidal_api_urls_from_gist() -> list[str]:
    resp = requests.get(_TIDAL_API_GIST_URL, timeout=10, headers={"User-Agent": _TIDAL_USER_AGENT})
    if resp.status_code != 200:
        raise RuntimeError(f"Tidal API gist returned status {resp.status_code}")
    urls = resp.json()
    if not isinstance(urls, list):
        raise RuntimeError("Tidal API gist did not return a JSON array")
    urls = _normalize_tidal_api_urls(urls)
    if not urls:
        raise RuntimeError("Tidal API gist returned no valid URLs")
    return urls


def _rotate_tidal_api_urls(urls: list[str], last_used_url: str) -> list[str]:
    normalized    = _normalize_tidal_api_urls(urls)
    last_used_url = last_used_url.strip().rstrip("/")
    if len(normalized) < 2 or not last_used_url:
        return normalized
    try:
        last_index = normalized.index(last_used_url)
    except ValueError:
        return normalized
    return normalized[last_index + 1:] + normalized[:last_index + 1]


def prime_tidal_api_list() -> None:
    try:
        refresh_tidal_api_list(force=True)
    except Exception as exc:
        logger.warning("[tidal] failed to refresh API list: %s", exc)
        # Fallback: salva almeno i builtin
        with _tidal_api_list_mu:
            state = _load_tidal_api_list_state_locked()
            if not state["urls"]:
                state["urls"]       = _normalize_tidal_api_urls(_TIDAL_APIS)
                state["updated_at"] = int(time.time())
                state["source"]     = "builtin-fallback"
                _save_tidal_api_list_state_locked(state)
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            logger.error("[tidal] API cache is empty after prime")

def refresh_tidal_api_list(force: bool = False) -> list[str]:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not force and state["urls"]:
            return list(state["urls"])
        try:
            gist_urls = _fetch_tidal_api_urls_from_gist()
        except Exception as exc:
            logger.warning("[tidal] gist fetch failed: %s", exc)
            gist_urls = []

        # Merge: builtin + gist, deduplicati, builtin prima
        merged = _normalize_tidal_api_urls(_TIDAL_APIS + gist_urls)

        if not merged:
            if state["urls"]:
                return list(state["urls"])
            raise RuntimeError("No Tidal API URLs available from any source")

        state["urls"]       = merged
        state["updated_at"] = int(time.time())
        state["source"]     = "builtin+gist"
        if state["last_used_url"] not in state["urls"]:
            state["last_used_url"] = ""
        _save_tidal_api_list_state_locked(state)
        return list(state["urls"])


def get_tidal_api_list() -> list[str]:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            raise RuntimeError("No cached Tidal API URLs")
        return list(state["urls"])


def get_rotated_tidal_api_list() -> list[str]:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            raise RuntimeError("No cached Tidal API URLs")
        return _rotate_tidal_api_urls(state["urls"], state["last_used_url"])


def remember_tidal_api_usage(api_url: str) -> None:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        state["last_used_url"] = api_url.strip().rstrip("/")
        if state["updated_at"] == 0:
            state["updated_at"] = int(time.time())
        _save_tidal_api_list_state_locked(state)


# ---------------------------------------------------------------------------
# Manifest parsing (invariato)
# ---------------------------------------------------------------------------

class ManifestResult(NamedTuple):
    direct_url: str
    init_url:   str
    media_urls: list[str]
    mime_type:  str


def parse_manifest(manifest_b64: str) -> ManifestResult:
    try:
        raw = base64.b64decode(manifest_b64)
    except Exception as exc:
        raise ParseError("tidal", f"failed to decode manifest: {exc}", exc)

    text = raw.decode(errors="ignore").strip()

    if text.startswith("{"):
        try:
            data = json.loads(text)
            urls = data.get("urls", [])
            mime = data.get("mimeType", "")
            if urls:
                return ManifestResult(urls[0], "", [], mime)
            raise ValueError("no URLs in BTS manifest")
        except Exception as exc:
            raise ParseError("tidal", f"BTS manifest parse failed: {exc}", exc)

    return _parse_dash_manifest(text)


def _parse_dash_manifest(text: str) -> ManifestResult:
    init_url = media_template = ""
    segment_count = 0

    try:
        mpd = ET.fromstring(text)
        ns  = {"mpd": mpd.tag.split("}")[0].strip("{")} if "}" in mpd.tag else {}
        seg = mpd.find(".//mpd:SegmentTemplate", ns) or mpd.find(".//SegmentTemplate")
        if seg is not None:
            init_url       = seg.get("initialization", "")
            media_template = seg.get("media", "")
            tl = seg.find("mpd:SegmentTimeline", ns) or seg.find("SegmentTimeline")
            if tl is not None:
                for s in (tl.findall("mpd:S", ns) or tl.findall("S")):
                    segment_count += int(s.get("r") or 0) + 1
    except Exception:
        pass

    if not init_url or not media_template or segment_count == 0:
        m_init  = re.search(r'initialization="([^"]+)"', text)
        m_media = re.search(r'media="([^"]+)"', text)
        if m_init:  init_url       = m_init.group(1)
        if m_media: media_template = m_media.group(1)
        for match in re.findall(r"<S\s+[^>]*>", text):
            r = re.search(r'r="(\d+)"', match)
            segment_count += int(r.group(1)) + 1 if r else 1

    if not init_url:
        raise ParseError("tidal", "no initialization URL found in DASH manifest")
    if segment_count == 0:
        raise ParseError("tidal", "no segments found in DASH manifest")

    init_url       = init_url.replace("&amp;", "&")
    media_template = media_template.replace("&amp;", "&")
    media_urls     = [media_template.replace("$Number$", str(i))
                      for i in range(1, segment_count + 1)]

    return ManifestResult("", init_url, media_urls, "")


# ---------------------------------------------------------------------------
# NUOVO: fetch singola API Tidal con retry + backoff esponenziale
# Porta fetchTidalURLWithRetry dal Go
# ---------------------------------------------------------------------------

def _fetch_tidal_url_once(
        api:       str,
        track_id:  int,
        quality:   str,
        timeout_s: int = _API_TIMEOUT_S,
) -> str:
    """
    Interroga una singola API Tidal con retry esponenziale.
    Porta fetchTidalURLWithRetry dal Go:
      - v2 response: ritorna 'MANIFEST:' + base64
      - v1 response: ritorna OriginalTrackUrl direttamente
      - retry su 5xx, 429, timeout, EOF
    """
    url       = f"{api.rstrip('/')}/track/?id={track_id}&quality={quality}"
    delay     = _RETRY_DELAY_S
    last_err: Exception = RuntimeError("no attempts made")

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            logger.debug("[tidal] retry %d/%d for %s after %.1fs", attempt, _MAX_RETRIES, api, delay)
            time.sleep(delay)
            delay *= 2

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _TIDAL_USER_AGENT},
                timeout=timeout_s,
            )

            if resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                continue
            if resp.status_code == 429:
                delay = max(delay, 2.0)
                last_err = RuntimeError("rate limited")
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")

            body = resp.text.strip()
            if not body:
                last_err = RuntimeError("empty response")
                continue

            try:
                data = resp.json()
            except ValueError:
                last_err = RuntimeError("invalid JSON")
                continue

            # Risposta v2 con manifest
            if isinstance(data, dict):
                manifest = data.get("data", {}).get("manifest", "")
                if manifest:
                    asset = data.get("data", {}).get("assetPresentation", "")
                    if asset == "PREVIEW":
                        raise RuntimeError("returned PREVIEW instead of FULL")
                    return "MANIFEST:" + manifest

            # Risposta v1 con lista di URL diretti
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("OriginalTrackUrl"):
                        return item["OriginalTrackUrl"]

            last_err = RuntimeError("no download URL or manifest in response")

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = exc
            continue
        except RuntimeError:
            raise
        except Exception as exc:
            last_err = exc
            break

    raise last_err


# ---------------------------------------------------------------------------
# NUOVO: fetch parallelo di tutte le API Tidal (prima risposta vince)
# Porta getDownloadURLParallel dal Go
# ---------------------------------------------------------------------------

def _fetch_tidal_url_parallel(
        apis:      list[str],
        track_id:  int,
        quality:   str,
        timeout_s: int = _API_TIMEOUT_S,
) -> tuple[str, str]:
    if not apis:
        raise SpotiflacError(ErrorKind.UNAVAILABLE, "no Tidal APIs configured", "tidal")

    start  = time.time()
    errors: list[str] = []

    pool = ThreadPoolExecutor(max_workers=min(len(apis), 8))
    try:
        futures: dict[Future, str] = {
            pool.submit(_fetch_tidal_url_once, api, track_id, quality, timeout_s): api
            for api in apis
        }
        for fut in as_completed(futures, timeout=timeout_s + 2):
            api = futures[fut]
            try:
                dl_url = fut.result()
                logger.debug("[tidal] parallel: got URL from %s in %.2fs", api, time.time() - start)
                # Shutdown senza aspettare i thread in esecuzione (Python 3.9+)
                pool.shutdown(wait=False, cancel_futures=True)
                return api, dl_url
            except Exception as exc:
                err_msg = str(exc)[:80]
                errors.append(f"{api}: {err_msg}")
                print_api_failure("tidal", api, err_msg)
    except (TimeoutError, FuturesTimeoutError):
        errors.append("global timeout exceeded")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    raise SpotiflacError(
        ErrorKind.UNAVAILABLE,
        f"all {len(apis)} Tidal APIs failed in {time.time()-start:.1f}s — {'; '.join(errors)}",
        "tidal",
    )


# ---------------------------------------------------------------------------
# TidalProvider
# ---------------------------------------------------------------------------

class TidalProvider(BaseProvider):
    name = "tidal"

    def __init__(
            self,
            apis:      list[str] | None = None,
            timeout_s: int              = 15,
    ) -> None:
        super().__init__(timeout_s=timeout_s, retry=RetryConfig(max_attempts=2))
        self._session = self._http._session
        self._session.headers.update({"User-Agent": self._random_ua()})

        try:
            prime_tidal_api_list()
            self._apis = apis or get_tidal_api_list()
        except Exception as exc:
            logger.warning("[tidal] API list unavailable, using built-in fallback: %s", exc)
            self._apis = list(apis or _TIDAL_APIS)

        self._qobuz_token: str | None = None

    # ------------------------------------------------------------------
    # Spotify → Tidal resolution (invariata)
    # ------------------------------------------------------------------

    def resolve_spotify_to_tidal(
            self,
            spotify_track_id: str,
            track_name:       str = "",
            artist_name:      str = "",
    ) -> str:
        if track_name and artist_name and track_name != "Unknown":
            result = self._search_on_mirrors(track_name, artist_name)
            if result:
                return result
        logger.info("[tidal] mirror search failed — trying Songlink")
        return self._resolve_via_songlink(spotify_track_id)

    def _search_on_mirrors(self, track_name: str, artist_name: str) -> str | None:
        clean_track  = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", track_name).strip()
        clean_artist = artist_name.split(",")[0].strip()
        query        = quote(f"{clean_artist} {clean_track}")

        for api in self._apis:
            base = api.rstrip("/")
            for endpoint in [
                f"{base}/search/?s={query}&limit=3",
                f"{base}/search?s={query}&limit=3",
                f"{base}/search/track/?s={query}&limit=3",
            ]:
                try:
                    resp = self._session.get(endpoint, timeout=7)
                    if resp.status_code != 200:
                        continue
                    t_id = self._extract_track_id(resp.json())
                    if t_id:
                        return f"https://listen.tidal.com/track/{t_id}"
                except Exception:
                    continue
        return None

    @staticmethod
    def _extract_track_id(data: object) -> str | None:
        if isinstance(data, list) and data:
            item = data[0]
            return str(item.get("id") or item.get("track_id") or "")
        if isinstance(data, dict):
            for key in ["items", "tracks", "result", "results"]:
                inner = data.get(key)
                if isinstance(inner, list) and inner:
                    return str(inner[0].get("id") or inner[0].get("track_id") or "")
            nested = data.get("data", {})
            if isinstance(nested, dict):
                for key in ["items", "tracks", "results"]:
                    inner = nested.get(key)
                    if isinstance(inner, list) and inner:
                        return str(inner[0].get("id") or inner[0].get("track_id") or "")
            direct = data.get("id") or data.get("trackId")
            if direct:
                return str(direct)
        return None

    def _resolve_via_songlink(self, spotify_track_id: str) -> str:
        resolver = LinkResolver(self._http)
        links = resolver.resolve_all(spotify_track_id)
        tidal_url = links.get("tidal")
        if tidal_url:
            return tidal_url
        raise TrackNotFoundError(self.name, spotify_track_id)

    # ------------------------------------------------------------------
    # NUOVO: download URL con fetch parallelo + fallback qualità
    # Porta GetDownloadURL + getDownloadURLParallel dal Go
    # ------------------------------------------------------------------

    def _get_download_url(self, track_id: int, quality: str) -> str:
        """
        Interroga tutte le API in parallelo con round-robin e provider_stats.
        Porta getDownloadURLParallel dal Go.
        """
        from ..core.provider_stats import prioritize_providers, record_success, record_failure

        try:
            rotated = get_rotated_tidal_api_list()
        except Exception:
            rotated = self._apis

        ordered = prioritize_providers("tidal", rotated)

        winner_api, dl_url = _fetch_tidal_url_parallel(ordered, track_id, quality, _API_TIMEOUT_S)
        record_success("tidal", winner_api)
        remember_tidal_api_usage(winner_api)
        print_source_banner("tidal", winner_api, quality)
        return dl_url

    def _get_download_url_with_fallback(self, track_id: int, quality: str) -> str:
        """Fallback HI_RES → LOSSLESS identico al Go."""
        try:
            return self._get_download_url(track_id, quality)
        except SpotiflacError:
            if quality == "HI_RES":
                print_quality_fallback("tidal", "HI_RES", "LOSSLESS")
                logger.warning("[tidal] HI_RES failed — fallback to LOSSLESS")
                return self._get_download_url(track_id, "LOSSLESS")
            raise

    # ------------------------------------------------------------------
    # File download (invariato)
    # ------------------------------------------------------------------

    def _download_file(self, url_or_manifest: str, dest: Path) -> None:
        if url_or_manifest.startswith("MANIFEST:"):
            self._download_from_manifest(url_or_manifest.removeprefix("MANIFEST:"), dest)
        else:
            self._http.stream_to_file(url_or_manifest, str(dest), self._progress_cb)

    def _download_from_manifest(self, manifest_b64: str, dest: Path) -> None:
        result = parse_manifest(manifest_b64)
        if result.direct_url and "flac" in result.mime_type.lower():
            self._http.stream_to_file(result.direct_url, str(dest), self._progress_cb)
            return

        tmp = dest.with_suffix(".m4a.tmp")
        try:
            if result.direct_url:
                self._http.stream_to_file(result.direct_url, str(tmp))
            else:
                self._download_segments(result.init_url, result.media_urls, tmp)
            self._ffmpeg_to_flac(tmp, dest)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _download_segments(self, init_url: str, media_urls: list[str], dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": _TIDAL_USER_AGENT}
        total   = len(media_urls)
        t_start = time.time()

        with open(dest, "wb") as f:
            resp = self._session.get(init_url, timeout=15, headers=headers)
            resp.raise_for_status()
            f.write(resp.content)

            for i, url in enumerate(media_urls, 1):
                resp = self._session.get(url, timeout=15, headers=headers)
                resp.raise_for_status()
                f.write(resp.content)
                pct    = i / total
                filled = int(pct * 24)
                bar    = "█" * filled + "░" * (24 - filled)
                eta    = (time.time() - t_start) / i * (total - i) if i > 0 else 0
                m, s   = divmod(int(eta), 60)
                print(f"\r  [{bar}] {i}/{total} segmenti  ETA {m:02d}:{s:02d}   ", end="", flush=True)

        elapsed = time.time() - t_start
        print(f"\r  ✓ {total} segmenti scaricati in {elapsed:.1f}s{'':<20}")

    @staticmethod
    def _ffmpeg_to_flac(src: Path, dst: Path) -> None:
        si = None
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "flac", str(dst)],
            capture_output=True, text=True, startupinfo=si,
        )
        if result.returncode != 0:
            m4a = dst.with_suffix(".m4a")
            src.rename(m4a)
            raise SpotiflacError(
                ErrorKind.FILE_IO,
                f"ffmpeg failed (M4A saved as {m4a.name}): {result.stderr}",
                "tidal",
            )

    # ------------------------------------------------------------------
    # Public download interface
    # ------------------------------------------------------------------

    def download_track(
            self,
            metadata:   TrackMetadata,
            output_dir: str,
            *,
            filename_format:     str  = "{title} - {artist}",
            position:            int  = 1,
            include_track_num:   bool = False,
            use_album_track_num: bool = False,
            first_artist_only:   bool = False,
            allow_fallback:      bool = True,
            quality:             str  = "LOSSLESS",
            embed_lyrics:            bool = False,
            lyrics_providers:        list[str] | None = None,
            lyrics_spotify_token:    str = "",
            lyrics_musixmatch_token: str = "",
            enrich_metadata:         bool = False,
            enrich_providers:        list[str] | None = None,
    ) -> DownloadResult:
        try:
            tidal_url = self.resolve_spotify_to_tidal(
                metadata.id, metadata.title, metadata.artists
            )
            track_id = self._parse_track_id(tidal_url)

            mb_fetcher = None
            if metadata.isrc:
                # Avviamo la ricerca mentre Tidal si prepara al download
                mb_fetcher = AsyncMBFetch(metadata.isrc)

            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            dl_url = (
                self._get_download_url_with_fallback(track_id, quality)
                if allow_fallback
                else self._get_download_url(track_id, quality)
            )

            self._download_file(dl_url, dest)

            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, err_msg, self.name)

            mb_tags = {}
            res: dict = {}
            if mb_fetcher:
                res = mb_fetcher.result()

            mapping = {
                "mbid_track":       "MUSICBRAINZ_TRACKID",
                "mbid_album":       "MUSICBRAINZ_ALBUMID",
                "mbid_artist":      "MUSICBRAINZ_ARTISTID",
                "mbid_relgroup":    "MUSICBRAINZ_RELEASEGROUPID",
                "mbid_albumartist": "MUSICBRAINZ_ALBUMARTISTID",
                "barcode":          "BARCODE",
                "label":            "LABEL",
                "organization":     "ORGANIZATION",
                "country":          "RELEASECOUNTRY",
                "script":           "SCRIPT",
                "status":           "RELEASESTATUS",
                "media":            "MEDIA",
                "type":             "RELEASETYPE",
                "artist_sort":      "ARTISTSORT",
                "albumartist_sort": "ALBUMARTISTSORT",
                "catalognumber":    "CATALOGNUMBER",
                "bpm":              "BPM",
                "genre":            "GENRE"
            }


            for mb_key, tag_name in mapping.items():
                val = res.get(mb_key)
                if val:
                    mb_tags[tag_name] = str(val)

            # Gestione speciale DATA ORIGINALE
            # Come richiesto, l'anno originale va nel campo DATE (sovrascrivendo quello Spotify)
            if res.get("original_date"):
                mb_tags["ORIGINALDATE"] = res["original_date"]
                mb_tags["ORIGINALYEAR"] = res["original_date"][:4]
            if res.get("catalognumber"):                         # ← FIX
                mb_tags["CATALOGNUMBER"] = res["catalognumber"]
            _print_mb_summary(mb_tags)
            # 6. Scrittura Metadati finali
            embed_metadata(
                dest, metadata,
                first_artist_only       = first_artist_only,
                cover_url               = metadata.cover_url,
                session                 = self._session,
                extra_tags              = mb_tags,
                embed_lyrics            = embed_lyrics,
                lyrics_providers        = lyrics_providers,
                lyrics_spotify_token    = lyrics_spotify_token,
                lyrics_musixmatch_token = lyrics_musixmatch_token,
                enrich                  = enrich_metadata,
                enrich_providers        = enrich_providers,
                enrich_qobuz_token      = self._qobuz_token or "",
            )
            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[tidal] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[tidal] unexpected error")
            return DownloadResult.fail(self.name, f"unexpected: {exc}")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_track_id(tidal_url: str) -> int:
        parts = tidal_url.split("/track/")
        if len(parts) < 2:
            raise ParseError("tidal", f"invalid Tidal URL: {tidal_url}")
        try:
            return int(parts[1].split("?")[0].strip())
        except ValueError as exc:
            raise ParseError("tidal", f"cannot parse track ID from {tidal_url}", exc)

    @staticmethod
    def _random_ua() -> str:
        from random import randrange
        return (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_{randrange(11,15)}_{randrange(4,9)}) "
            f"AppleWebKit/{randrange(530,537)}.{randrange(30,37)} (KHTML, like Gecko) "
            f"Chrome/{randrange(80,105)}.0.{randrange(3000,4500)}.{randrange(60,125)} "
            f"Safari/{randrange(530,537)}.{randrange(30,36)}"
        )
"""
TidalProvider — refactored.

Cambiamenti rispetto al codice originale:
- Lista API come attributo d'istanza, non hard-coded in ogni metodo
- Rotation system deterministicamente shuffled (non globale)
- Risoluzione Spotify→Tidal con retry puliti
- parse_manifest estratta come funzione pura testabile
- Tutti gli errori sono SpotiflacError tipati
- embed_metadata delegato al tagger centralizzato
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
from ..core.tagger import embed_metadata
from .base import BaseProvider
from ..core.console import (
    print_source_banner, print_api_failure,
    print_quality_fallback,
)
from ..core.download_validation import validate_downloaded_track

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIDAL_APIS = [
    "https://api.monochrome.tf",
    "https://tidal-api.binimum.org",
    "https://eu-central.monochrome.tf",
    "https://triton.squid.wtf",
    "https://monochrome-api.samidy.com",
    "https://hifi-two.spotisaver.net",
    "https://hifi.geeked.wtf",
    "https://us-west.monochrome.tf"
]

_TIDAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Tidal API List Manager (port del modulo Go)
# ---------------------------------------------------------------------------

_TIDAL_API_GIST_URL   = "https://gist.githubusercontent.com/afkarxyz/2ce772b943321b9448b454f39403ce25/raw"
_TIDAL_API_CACHE_FILE = "tidal-api-urls.json"

_tidal_api_list_mu:    threading.Lock              = threading.Lock()
_tidal_api_list_state: dict | None                 = None  # equivalente a tidalAPIListState


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
    """Rimuove duplicati e slash finali — equivalente a normalizeTidalAPIURLs."""
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
    """Equivalente a loadTidalAPIListStateLocked."""
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
        logger.warning("[tidal] Failed to read API list cache: %s", exc)
        empty = {"urls": [], "last_used_url": "", "updated_at": 0, "source": ""}
        _tidal_api_list_state = _clone_state(empty)
        return _clone_state(empty)


def _save_tidal_api_list_state_locked(state: dict) -> None:
    """Equivalente a saveTidalAPIListStateLocked."""
    global _tidal_api_list_state
    cache_path = _get_cache_path()
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        _tidal_api_list_state = _clone_state(state)
    except Exception as exc:
        logger.warning("[tidal] Failed to write API list cache: %s", exc)


def _fetch_tidal_api_urls_from_gist() -> list[str]:
    """Equivalente a fetchTidalAPIURLsFromGist."""
    resp = requests.get(_TIDAL_API_GIST_URL, timeout=12, headers={
        "User-Agent": _TIDAL_USER_AGENT,
    })
    if resp.status_code != 200:
        raise RuntimeError(
            f"Tidal API gist returned status {resp.status_code}: {resp.text[:200]}"
        )
    urls = resp.json()
    if not isinstance(urls, list):
        raise RuntimeError("Tidal API gist did not return a JSON array")
    urls = _normalize_tidal_api_urls(urls)
    if not urls:
        raise RuntimeError("Tidal API gist returned no valid URLs")
    return urls


def _rotate_tidal_api_urls(urls: list[str], last_used_url: str) -> list[str]:
    """Equivalente a rotateTidalAPIURLs — round-robin partendo dal successivo."""
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
    """
    Chiamato all'avvio — aggiorna la lista dal Gist e verifica la cache.
    Equivalente a PrimeTidalAPIList.
    """
    try:
        refresh_tidal_api_list(force=True)
    except Exception as exc:
        logger.warning("[tidal] Failed to refresh API list from gist: %s", exc)

    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            logger.error("[tidal] API cache is empty after prime")
            return
        if state["updated_at"] == 0:
            state["updated_at"] = int(time.time())
            _save_tidal_api_list_state_locked(state)


def refresh_tidal_api_list(force: bool = False) -> list[str]:
    """
    Scarica la lista dal Gist e aggiorna la cache.
    Equivalente a RefreshTidalAPIList.
    """
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()

        if not force and state["urls"]:
            return list(state["urls"])

        try:
            urls = _fetch_tidal_api_urls_from_gist()
        except Exception as exc:
            logger.warning("[tidal] Gist fetch failed: %s", exc)
            if state["urls"]:
                return list(state["urls"])
            raise

        state["urls"]       = urls
        state["updated_at"] = int(time.time())
        state["source"]     = "gist"

        # invalida last_used_url se non è più nella lista
        if state["last_used_url"] not in state["urls"]:
            state["last_used_url"] = ""

        _save_tidal_api_list_state_locked(state)
        return list(state["urls"])


def get_tidal_api_list() -> list[str]:
    """Equivalente a GetTidalAPIList."""
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            raise RuntimeError("No cached Tidal API URLs")
        return list(state["urls"])


def get_rotated_tidal_api_list() -> list[str]:
    """Equivalente a GetRotatedTidalAPIList — round-robin basato su last_used."""
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            raise RuntimeError("No cached Tidal API URLs")
        return _rotate_tidal_api_urls(state["urls"], state["last_used_url"])


def remember_tidal_api_usage(api_url: str) -> None:
    """Equivalente a RememberTidalAPIUsage — persiste l'ultimo URL usato."""
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        state["last_used_url"] = api_url.strip().rstrip("/")
        if state["updated_at"] == 0:
            state["updated_at"] = int(time.time())
        _save_tidal_api_list_state_locked(state)

# ---------------------------------------------------------------------------
# Manifest parsing (pure function, testabile)
# ---------------------------------------------------------------------------

class ManifestResult(NamedTuple):
    direct_url:     str
    init_url:       str
    media_urls:     list[str]
    mime_type:      str


def parse_manifest(manifest_b64: str) -> ManifestResult:
    """
    Decodifica un manifest Base64 in uno dei due formati supportati:
    - BTS (JSON): ritorna direct_url + mime_type
    - DASH (XML): ritorna init_url + media_urls per il download segmentato
    """
    try:
        raw = base64.b64decode(manifest_b64)
    except Exception as exc:
        raise ParseError("tidal", f"Failed to decode manifest: {exc}", exc)

    text = raw.decode(errors="ignore").strip()

    # BTS format (JSON)
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

    # DASH format (XML)
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

    # Regex fallback
    if not init_url or not media_template or segment_count == 0:
        m_init  = re.search(r'initialization="([^"]+)"', text)
        m_media = re.search(r'media="([^"]+)"', text)
        if m_init:  init_url       = m_init.group(1)
        if m_media: media_template = m_media.group(1)
        for match in re.findall(r"<S\s+[^>]*>", text):
            r = re.search(r'r="(\d+)"', match)
            segment_count += int(r.group(1)) + 1 if r else 1

    if not init_url:
        raise ParseError("tidal", "No initialization URL found in DASH manifest")
    if segment_count == 0:
        raise ParseError("tidal", "No segments found in DASH manifest")

    init_url       = init_url.replace("&amp;", "&")
    media_template = media_template.replace("&amp;", "&")
    media_urls     = [media_template.replace("$Number$", str(i))
                      for i in range(1, segment_count + 1)]

    return ManifestResult("", init_url, media_urls, "")


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

        # Carica la lista dal Gist all'avvio; fallback sugli hard-coded
        try:
            prime_tidal_api_list()
            self._apis = apis or get_tidal_api_list()
        except Exception as exc:
            logger.warning("[tidal] API list unavailable, using built-in fallback: %s", exc)
            self._apis = list(apis or _TIDAL_APIS)

    # ------------------------------------------------------------------
    # Spotify → Tidal resolution
    # ------------------------------------------------------------------

    def resolve_spotify_to_tidal(
            self,
            spotify_track_id: str,
            track_name:       str = "",
            artist_name:      str = "",
    ) -> str:
        """
        Ritorna URL Tidal per il track_id Spotify.
        Prima prova la ricerca testuale sulle API mirror,
        poi fallback su Songlink.
        """
        if track_name and artist_name and track_name != "Unknown":
            result = self._search_on_mirrors(track_name, artist_name)
            if result:
                return result

        logger.info("[tidal] Mirror search failed — trying Songlink")
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
                        logger.debug("[tidal] Found via %s → ID %s", base, t_id)
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
            # Nested data key
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
        spotify_url = f"https://open.spotify.com/track/{spotify_track_id}"
        for country in ("US", "BR", "IT"):
            url = (
                "https://api.song.link/v1-alpha.1/links?"
                f"url={quote(spotify_url, safe='')}&userCountry={country}"
            )
            try:
                resp = self._session.get(url, timeout=10)
                resp.raise_for_status()
                links = resp.json().get("linksByPlatform", {})
                for platform in ("tidal", "tidalAlt"):
                    tidal = links.get(platform) or {}
                    link = str(tidal.get("url", "")).strip()
                    if link:
                        return link
            except Exception as exc:
                logger.debug("[tidal] song.link resolve failed (%s): %s", country, exc)
        raise TrackNotFoundError(self.name, spotify_track_id)

    # ------------------------------------------------------------------
    # Download URL (rotation system)
    # ------------------------------------------------------------------

    def _get_download_url(self, track_id: int, quality: str) -> str:
        from ..core.provider_stats import prioritize_providers, record_success, record_failure

        # Usa la rotazione round-robin dal gestore dinamico
        try:
            rotated = get_rotated_tidal_api_list()
        except Exception:
            rotated = self._apis

        ordered = prioritize_providers("tidal", rotated)

        last_err: str = ""
        for api in ordered:
            url = f"{api}/track/?id={track_id}&quality={quality}"
            try:
                resp = self._session.get(
                    url, timeout=15,
                    headers={"User-Agent": _TIDAL_USER_AGENT},
                )
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}"
                    record_failure("tidal", api)
                    print_api_failure("tidal", api, last_err)
                    continue

                body = resp.json()
                if isinstance(body, dict) and body.get("data", {}).get("manifest"):
                    record_success("tidal", api)
                    remember_tidal_api_usage(api)  # ← persiste per il round-robin
                    print_source_banner("tidal", api, quality)
                    return "MANIFEST:" + body["data"]["manifest"]
                if isinstance(body, list):
                    for item in body:
                        if item.get("OriginalTrackUrl"):
                            record_success("tidal", api)
                            remember_tidal_api_usage(api)  # ← persiste per il round-robin
                            print_source_banner("tidal", api, quality)
                            return item["OriginalTrackUrl"]

                last_err = "no URL in response"
                record_failure("tidal", api)
                print_api_failure("tidal", api, last_err)

            except Exception as exc:
                last_err = str(exc)
                record_failure("tidal", api)
                print_api_failure("tidal", api, last_err)
                logger.debug("[tidal] %s → %s", api, exc)

        raise SpotiflacError(
            ErrorKind.UNAVAILABLE,
            f"All Tidal APIs failed (last: {last_err})",
            self.name,
        )

    def _get_download_url_with_fallback(self, track_id: int, quality: str) -> str:
        try:
            return self._get_download_url(track_id, quality)
        except SpotiflacError:
            if quality == "HI_RES":
                print_quality_fallback("tidal", "HI_RES", "LOSSLESS")
                logger.warning("[tidal] HI_RES failed — fallback to LOSSLESS")
                return self._get_download_url(track_id, "LOSSLESS")
            raise

    # ------------------------------------------------------------------
    # File download (direct or manifest)
    # ------------------------------------------------------------------

    def _download_file(self, url_or_manifest: str, dest: Path) -> None:
        if url_or_manifest.startswith("MANIFEST:"):
            self._download_from_manifest(
                url_or_manifest.removeprefix("MANIFEST:"), dest
            )
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
        _headers = {"User-Agent": _TIDAL_USER_AGENT}
        total    = len(media_urls)
        t_start  = time.time()

        with open(dest, "wb") as f:
            resp = self._session.get(init_url, timeout=20, headers=_headers)
            resp.raise_for_status()
            f.write(resp.content)

            for i, url in enumerate(media_urls, 1):
                resp = self._session.get(url, timeout=20, headers=_headers)
                resp.raise_for_status()
                f.write(resp.content)

                pct     = i / total
                filled  = int(pct * 24)
                bar     = "█" * filled + "░" * (24 - filled)
                elapsed = time.time() - t_start
                eta     = (elapsed / i) * (total - i) if i > 0 else 0
                m, s    = divmod(int(eta), 60)
                print(
                    f"\r  [{bar}] {i}/{total} segmenti  ETA {m:02d}:{s:02d}   ",
                    end="", flush=True,
                )

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
            capture_output=True,
            text=True,
            startupinfo=si,
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
    ) -> DownloadResult:
        try:
            tidal_url = self.resolve_spotify_to_tidal(
                metadata.id, metadata.title, metadata.artists
            )
            track_id = self._parse_track_id(tidal_url)

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

            embed_metadata(
                dest, metadata,
                first_artist_only=first_artist_only,
                cover_url=metadata.cover_url,
                session=self._session,
            )

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[tidal] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[tidal] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_track_id(tidal_url: str) -> int:
        parts = tidal_url.split("/track/")
        if len(parts) < 2:
            raise ParseError("tidal", f"Invalid Tidal URL: {tidal_url}")
        try:
            return int(parts[1].split("?")[0].strip())
        except ValueError as exc:
            raise ParseError("tidal", f"Cannot parse track ID from {tidal_url}", exc)

    @staticmethod
    def _random_ua() -> str:
        from random import randrange
        return (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_{randrange(11,15)}_{randrange(4,9)}) "
            f"AppleWebKit/{randrange(530,537)}.{randrange(30,37)} (KHTML, like Gecko) "
            f"Chrome/{randrange(80,105)}.0.{randrange(3000,4500)}.{randrange(60,125)} "
            f"Safari/{randrange(530,537)}.{randrange(30,36)}"
        )

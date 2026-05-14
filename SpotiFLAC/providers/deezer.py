# deezer_provider.py
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any

import requests

from ..core.tagger import embed_metadata, EmbedOptions

# Gestione opzionale di pycryptodome per la decrittazione Blowfish
try:
    from Crypto.Cipher import Blowfish
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError, ErrorKind
from .base import BaseProvider
from ..core.musicbrainz import mb_result_to_tags

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_MAX_RETRIES   = 2
_RETRY_DELAY_S = 0.5
_API_TIMEOUT_S = 15

_CACHE_TTL_S              = 10 * 60
_CACHE_CLEANUP_INTERVAL_S = 5  * 60
_MAX_TRACK_CACHE          = 4000
_MAX_SEARCH_CACHE         = 300

_RETRYABLE_SUBSTRINGS = (
    "timeout", "connection reset", "connection refused", "EOF",
    "status 5", "status 429", "RemoteDisconnected",
)

# Costanti per la decrittazione
_BLOWFISH_SECRET = b"g4el58wc0zvf9na1"
_BLOWFISH_IV = bytes.fromhex("0001020304050607")
_CHUNK_SIZE = 2048
_RESOLVER_URL = "https://api.zarz.moe/v1/dl/dzr"


class _CacheEntry:
    __slots__ = ("data", "expires_at")

    def __init__(self, data: Any, ttl_s: float = _CACHE_TTL_S) -> None:
        self.data       = data
        self.expires_at = time.monotonic() + ttl_s

    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class DeezerProvider(BaseProvider):
    name = "deezer"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})

        self._track_cache:  dict[str, _CacheEntry] = {}
        self._search_cache: dict[str, _CacheEntry] = {}
        self._cache_mu              = threading.Lock()
        self._url_locks             = {}
        self._last_cache_cleanup    = 0.0

        if not HAS_CRYPTO:
            logger.warning("[deezer] pycryptodome not found. Execute 'pip install pycryptodome'.")

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _maybe_cleanup_cache(self) -> None:
        now = time.monotonic()
        if now - self._last_cache_cleanup < _CACHE_CLEANUP_INTERVAL_S:
            return
        self._last_cache_cleanup = now
        for cache in (self._track_cache, self._search_cache):
            expired = [k for k, v in cache.items() if v.is_expired()]
            for k in expired:
                del cache[k]
        self._trim_cache(self._track_cache,  _MAX_TRACK_CACHE)
        self._trim_cache(self._search_cache, _MAX_SEARCH_CACHE)

    @staticmethod
    def _trim_cache(cache: dict, max_entries: int) -> None:
        if len(cache) <= max_entries:
            return
        sorted_keys = sorted(cache, key=lambda k: cache[k].expires_at)
        for k in sorted_keys[:len(cache) - max_entries]:
            del cache[k]

    # ------------------------------------------------------------------
    # HTTP con retry
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict:
        last_err: Exception | None = None
        delay = _RETRY_DELAY_S

        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                time.sleep(delay)
                delay *= 2
            try:
                resp = self._session.get(url, timeout=_API_TIMEOUT_S)
                if resp.status_code == 429:
                    delay    = max(delay, 2.0)
                    last_err = RuntimeError("rate limited (429)")
                    continue
                if resp.status_code >= 500:
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_err = exc
                continue
            except Exception as exc:
                if any(s in str(exc) for s in _RETRYABLE_SUBSTRINGS):
                    last_err = exc
                    continue
                raise RuntimeError(f"Deezer request failed: {exc}") from exc

        raise RuntimeError(f"All {_MAX_RETRIES + 1} attempts failed: {last_err}")

    def _get_json_cached(self, url: str) -> dict:
        with self._cache_mu:
            entry = self._search_cache.get(url)
            if entry and not entry.is_expired():
                return entry.data
            self._maybe_cleanup_cache()

            if url not in self._url_locks:
                self._url_locks[url] = threading.Lock()
            url_lock = self._url_locks[url]

        with url_lock:
            with self._cache_mu:
                entry = self._search_cache.get(url)
                if entry and not entry.is_expired():
                    return entry.data
            try:
                data = self._get_json(url)
                with self._cache_mu:
                    self._search_cache[url] = _CacheEntry(data)
                return data
            finally:
                with self._cache_mu:
                    self._url_locks.pop(url, None)

    def _post_json(self, url: str, payload: dict) -> dict:
        import time

        # Selettore User-Agent come nel file JS
        is_zarz = url.startswith("https://api.zarz.moe") or url.startswith("http://api.zarz.moe")
        headers = {
            "User-Agent": "SpotiFLAC-Mobile" if is_zarz else _DEFAULT_UA,
            "Accept": "application/json"
        }

        max_retries = 2
        base_delay = 3.0

        for attempt in range(max_retries):
            resp = self._session.post(url, json=payload, headers=headers, timeout=_API_TIMEOUT_S)

            if resp.status_code == 429:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "[deezer] HTTP 429 on %s: rate limit, waiting %.1f seconds (attempt %d/%d)...",
                    url, delay, attempt + 1, max_retries
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()
            return resp.json()

        # Se esaurisce i tentativi, solleva l'eccezione dell'ultima risposta
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # API Deezer
    # ------------------------------------------------------------------

    def _get_track_by_isrc(self, isrc: str) -> dict | None:
        with self._cache_mu:
            entry = self._track_cache.get(isrc)
            if entry and not entry.is_expired():
                return entry.data
        try:
            data = self._get_json(f"https://api.deezer.com/2.0/track/isrc:{isrc}")
            if "error" in data:
                logger.warning("[deezer] API error: %s", data["error"].get("message", "?"))
                return None
            with self._cache_mu:
                self._track_cache[isrc] = _CacheEntry(data)
                self._maybe_cleanup_cache()
            return data
        except Exception as exc:
            logger.warning("[deezer] get_track_by_isrc failed: %s", exc)
            return None

    def _search_track_text(self, title: str, artist: str) -> dict | None:
        """
        Fallback avanzato: cerca la traccia per testo su Deezer valutando i risultati
        con un sistema a punteggio (70% titolo, 30% artista), basato sulla logica JS.
        """
        import urllib.parse
        import difflib # Aggiunto per confrontare le stringhe

        first_artist = artist.split(",")[0].strip()
        query = f'track:"{title}" artist:"{first_artist}"'
        # Aumentiamo il limite per avere più candidati, come nel JS
        url = f"https://api.deezer.com/search?q={urllib.parse.quote(query)}&limit=10"

        try:
            data = self._get_json_cached(url)
            if data and data.get("data"):
                best_match = None
                best_score = 0.0

                title_lower = title.lower()
                artist_lower = first_artist.lower()

                for track in data["data"]:
                    t_title = track.get("title", "").lower()
                    t_artist = track.get("artist", {}).get("name", "").lower()

                    # Calcolo del punteggio (simile a findBestSearchMatch del JS)
                    title_ratio = difflib.SequenceMatcher(None, title_lower, t_title).ratio()
                    artist_ratio = difflib.SequenceMatcher(None, artist_lower, t_artist).ratio()

                    score = (title_ratio * 70) + (artist_ratio * 30)

                    if score > best_score:
                        best_score = score
                        best_match = track

                # Se il punteggio supera la soglia minima di 55 (come nel JS)
                if best_match and best_score >= 55:
                    track_id = best_match.get("id")
                    if track_id:
                        logger.debug("[deezer] Trovata corrispondenza testuale con score %.2f", best_score)
                        return self._get_json(f"https://api.deezer.com/track/{track_id}")
                else:
                    logger.debug("[deezer] Nessuna traccia ha superato lo score minimo (Migliore: %.2f)", best_score)

        except Exception as e:
            logger.debug("[deezer] Ricerca testuale avanzata fallita: %s", e)

        return None

    # ------------------------------------------------------------------
    # Metadati & Crypto Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _best_cover(album: dict) -> str:
        return (
                album.get("cover_xl") or album.get("cover_big") or
                album.get("cover_medium") or album.get("cover") or ""
        )

    @staticmethod
    def _track_artist_display(track_data: dict) -> str:
        contributors = track_data.get("contributors", [])
        if contributors:
            return ", ".join(c["name"] for c in contributors if c.get("name"))
        return track_data.get("artist", {}).get("name", "")

    def _extract_metadata(self, track_data: dict) -> dict:
        album = track_data.get("album", {})
        return {
            "title":          track_data.get("title", ""),
            "track_position": track_data.get("track_position", 1),
            "disk_number":    track_data.get("disk_number", 1),
            "isrc":           track_data.get("isrc", ""),
            "release_date":   track_data.get("release_date", ""),
            "artist":         track_data.get("artist", {}).get("name", ""),
            "artists":        self._track_artist_display(track_data),
            "album":          album.get("title", ""),
            "cover_url":      self._best_cover(album),
        }

    @staticmethod
    def _safe(s: str) -> str:
        return "".join(c for c in s if c.isalnum() or c in " -_").strip()

    @staticmethod
    def _generate_blowfish_key(track_id: str) -> bytes:
        md5_hex = hashlib.md5(str(track_id).encode('ascii')).hexdigest().encode('ascii')
        key = bytearray(16)
        for i in range(16):
            key[i] = md5_hex[i] ^ md5_hex[i + 16] ^ _BLOWFISH_SECRET[i]
        return bytes(key)

    def _decrypt_file(self, encrypted_path: str, output_path: str, track_id: str) -> bool:
        # FIX #1: SpotiflacError chiamata con firma corretta (kind, message)
        if not HAS_CRYPTO:
            raise SpotiflacError(ErrorKind.FILE_IO, "Manca pycryptodome, impossibile decriptare la traccia.")

        key = self._generate_blowfish_key(track_id)

        try:
            with open(encrypted_path, "rb") as f_in, open(output_path, "wb") as f_out:
                chunk_index = 0
                while True:
                    chunk = f_in.read(_CHUNK_SIZE)
                    if not chunk:
                        break

                    # Deezer cripta solo 1 blocco ogni 3 (0, 3, 6...) se è pieno
                    if len(chunk) == _CHUNK_SIZE and chunk_index % 3 == 0:
                        cipher = Blowfish.new(key, Blowfish.MODE_CBC, _BLOWFISH_IV)  # reset per ogni chunk
                        decrypted = cipher.decrypt(chunk)
                        f_out.write(decrypted)
                    else:
                        f_out.write(chunk)

                    chunk_index += 1
            return True
        except Exception as exc:
            logger.error("[deezer] Decrittazione fallita: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Download raw FLAC tramite API
    # ------------------------------------------------------------------

    def _download_flac_raw(self, isrc: str, output_dir: str) -> str | None:
        track_data = self._get_track_by_isrc(isrc)
        if not track_data:
            return None

        meta     = self._extract_metadata(track_data)
        track_id = track_data.get("id")
        if not track_id:
            return None

        logger.info("[deezer] Found: %s - %s (ID: %s)", meta["artists"], meta["title"], track_id)

        try:
            payload = {
                "platform": "deezer",
                "url": f"https://www.deezer.com/track/{track_id}"
            }
            api_data = self._post_json(_RESOLVER_URL, payload)

            if not api_data.get("success"):
                logger.warning("[deezer] Impossibile risolvere il link: %s", api_data.get("message", "Errore sconosciuto"))
                return None

            download_url = api_data.get("direct_download_url") or api_data.get("download_url")
            if not download_url:
                logger.warning("[deezer] Nessun URL di download restituito dal resolver.")
                return None

            requires_decryption = api_data.get("requires_client_decryption", False)
            if not requires_decryption and api_data.get("direct_downloadable") is False:
                requires_decryption = True
            if api_data.get("deezer_encrypted", False):
                requires_decryption = True

        except Exception as exc:
            logger.warning("[deezer] Resolver API failed:: %s", exc)
            return None

        filename  = f"{self._safe(meta['artists'])} - {self._safe(meta['title'])}.flac"
        file_path = os.path.join(output_dir, filename)
        temp_path = file_path + ".encrypted" if requires_decryption else file_path

        try:
            os.makedirs(output_dir, exist_ok=True)
            with self._session.get(download_url, stream=True, timeout=_API_TIMEOUT_S) as resp:
                resp.raise_for_status()
                total    = int(resp.headers.get("content-length", 0))
                received = 0
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            received += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(received, total)
        except Exception as exc:
            logger.warning("[deezer] Download fallito: %s", exc)
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None

        if requires_decryption:
            logger.info("[deezer] File criptato rilevato. Inizio decrittazione Blowfish...")
            success = self._decrypt_file(temp_path, file_path, str(track_id))
            if os.path.exists(temp_path):
                os.remove(temp_path)

            if not success:
                if os.path.exists(file_path):
                    os.remove(file_path)
                return None

        return file_path

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot(directory: str) -> set[str]:
        result = set()
        for root, _, files in os.walk(directory):
            for f in files:
                if f.lower().endswith(".flac"):
                    result.add(os.path.join(root, f))
        return result

    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            quality:             str             = "flac",
            filename_format:     str             = "{title} - {artist}",
            position:            int             = 1,
            include_track_num:   bool            = False,
            use_album_track_num: bool            = False,
            first_artist_only:   bool            = False,
            allow_fallback:      bool            = True,
            embed_lyrics:        bool            = False,
            lyrics_providers:    list[str] | None = None,
            enrich_metadata:     bool            = False,
            enrich_providers:    list[str] | None = None,
            qobuz_token:         str | None       = None,
            is_album:            bool            = False,
            **kwargs,
    ) -> DownloadResult:

        isrc_to_use = metadata.isrc

        if not isrc_to_use:
            logger.warning("[deezer] ISRC mancante in metadata. Tento ricerca testuale per: %s - %s", metadata.title, metadata.artists)
            fallback_track = self._search_track_text(metadata.title, metadata.artists)
            if fallback_track and fallback_track.get("isrc"):
                isrc_to_use = fallback_track["isrc"]
                logger.info("[deezer] Found alternative ISRC: %s", isrc_to_use)
            else:
                return DownloadResult.fail(self.name, "Nessun ISRC disponibile e ricerca testuale fallita.")

        try:
            dest = self._build_output_path(
                metadata,
                output_dir,
                filename_format=filename_format,
                position=position,
                include_track_num=include_track_num,
                use_album_track_num=use_album_track_num,
                first_artist_only=first_artist_only,
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            from ..core.musicbrainz import AsyncMBFetch
            mb_fetcher = AsyncMBFetch(isrc_to_use) if isrc_to_use else None

            try:
                from ..core.console import print_source_banner
                print_source_banner("Deezer", _RESOLVER_URL, quality.upper())
            except ImportError:
                pass

            downloaded = self._download_flac_raw(isrc_to_use, output_dir)

            if not downloaded or not os.path.exists(downloaded):
                return DownloadResult.fail(self.name, "No FLAC file downloaded")

            if os.path.abspath(downloaded) != os.path.abspath(str(dest)):
                import shutil
                os.makedirs(os.path.dirname(str(dest)), exist_ok=True)
                shutil.move(downloaded, str(dest))

            from ..core.download_validation import validate_downloaded_track
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                return DownloadResult.fail(self.name, f"Validazione fallita: {err_msg}")

            mb_tags: dict[str, str] = {}
            res: dict = {}
            if mb_fetcher:
                res = mb_fetcher.future.result()

            mb_tags = mb_result_to_tags(res)

            try:
                from ..core.tagger import _print_mb_summary
                _print_mb_summary(mb_tags)
            except ImportError:
                pass

            opts = EmbedOptions(
                first_artist_only       = first_artist_only,
                cover_url               = metadata.cover_url,
                extra_tags              = mb_tags,
                embed_lyrics            = embed_lyrics,
                lyrics_providers        = lyrics_providers or [],
                enrich                  = enrich_metadata,
                enrich_providers        = enrich_providers,
                enrich_qobuz_token      = qobuz_token or "",
                is_album                = is_album,
            )
            embed_metadata(str(dest), metadata, opts, session=self._session)

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[deezer] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[deezer] Errore imprevisto")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
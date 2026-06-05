from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .base import BaseProvider
from ..core.console import print_source_banner
from ..core.http import NetworkManager
from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError, ErrorKind, TrackNotFoundError
from ..core.tagger import embed_metadata, EmbedOptions
from ..core.download_validation import validate_downloaded_track
from ..core.musicbrainz import AsyncMBFetch, mb_result_to_tags

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Endpoint dell'API
_API_BASE   = "https://music.wjhe.top/api.php"
_API_STREAM = "https://music.wjhe.top/api/music/migu/url"
_SOURCE     = "migu"


class MiguProvider(BaseProvider):
    """
    Provider per Migu Music tramite l'API unificata (music.wjhe.top).
    
    Gestisce rigorosamente e in modo esclusivo audio Lossless e Hi-Res (FLAC, M4A).
    Gli MP3 o i formati a bassa qualità non sono supportati e verranno rifiutati.
    """

    name = "migu"

    def __init__(self, timeout_s: int = 30) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = NetworkManager.get_sync_client()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})

    # ------------------------------------------------------------------
    # Internal API helpers
    # ------------------------------------------------------------------

    def _search(self, query: str, count: int = 10) -> list[dict]:
        """Cerca tracce su Migu tramite l'API specificata."""
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "search",
                    "source": _SOURCE,
                    "name":   query,
                    "count":  count,
                    "pages":  1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("data", data.get("result", []))
        except Exception as exc:
            logger.debug("[migu] Search failed for '%s': %s", query, exc)
        return []

    def _get_stream(self, track_id: str, quality_pref: str) -> tuple[str, str, str]:
        """
        Richiede un URL di stream per Migu basandosi sulla qualità richiesta.
        Implementa il fallback gerarchico 24-bit -> 16-bit per formati FLAC/M4A.
        Rifiuta attivamente MP3 e URL lossy generici.
        
        Ritorna una tupla: (url_stream, estensione_file, etichetta_qualità).
        """
        
        # Ordine di tentativi strettamente ad alta qualità
        if quality_pref.upper() in ["LOSSLESS", "HI-RES", "FLAC", "MAX"]:
            attempts = [
                (3000, "flac", "FLAC 24-bit"),
                (1000, "flac", "FLAC 16-bit"),
                (3000, "m4a",  "M4A 24-bit"),
                (1000, "m4a",  "M4A 16-bit"),
            ]
        else:
            attempts = [
                (1000, "flac", "FLAC 16-bit"),
                (1000, "m4a",  "M4A 16-bit"),
            ]
            
        for q_val, fmt, label in attempts:
            try:
                resp = self._session.get(
                    _API_STREAM,
                    params={
                        "ID": track_id,
                        "quality": q_val,
                        "format": fmt
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                
                dl_url = ""
                # Gestione della risposta: potrebbe essere JSON annidato o una stringa raw
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        dl_url = data.get("url")
                        if not dl_url:
                            inner = data.get("data")
                            if isinstance(inner, str):
                                dl_url = inner
                            elif isinstance(inner, dict):
                                dl_url = inner.get("url", "")
                except ValueError:
                    text_resp = resp.text.strip()
                    if text_resp.startswith("http"):
                        dl_url = text_resp
                        
                # Verifica che l'URL sia valido e non porti a un MP3 di errore ("404.mp3")
                if dl_url and isinstance(dl_url, str) and dl_url.startswith("http"):
                    if "404" not in dl_url and "null" not in dl_url.lower():
                        logger.debug("[migu] Found stream using quality=%d format=%s", q_val, fmt)
                        return dl_url, f".{fmt}", label
                        
            except Exception as exc:
                logger.debug("[migu] Attempt quality=%d format=%s failed: %s", q_val, fmt, exc)
                
        # Fallback estremo all'API di ricerca URL generica qualora l'endpoint specifico fallisca del tutto
        try:
            resp = self._session.get(
                _API_BASE,
                params={"types": "url", "source": _SOURCE, "id": track_id, "br": 999},
                timeout=10
            )
            data = resp.json()
            url = data.get("url", "")
            if url and "404" not in url:
                if "flac" in url.lower():
                    return url, ".flac", "FLAC (Fallback)"
                elif "m4a" in url.lower() or "mp4" in url.lower():
                    return url, ".m4a", "M4A (Fallback)"
                else:
                    logger.debug("[migu] Fallback URL returned lossy MP3 - rejected")
        except Exception:
            pass

        return "", "", ""

    def _get_pic_url(self, pic_id: str, size: int = 500) -> str:
        """Recupera l'URL della copertina dell'album."""
        if not pic_id:
            return ""
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "pic",
                    "source": _SOURCE,
                    "id":     pic_id,
                    "size":   size,
                },
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json().get("url", "")
        except Exception as exc:
            logger.debug("[migu] Pic fetch failed for pic_id=%s: %s", pic_id, exc)
        return ""

    def _get_lyric(self, lyric_id: str) -> str:
        """Recupera il testo della canzone (LRC)."""
        if not lyric_id:
            return ""
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "lyric",
                    "source": _SOURCE,
                    "id":     lyric_id,
                },
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("lyric", "") or data.get("tlyric", "")
        except Exception as exc:
            logger.debug("[migu] Lyric fetch failed for id=%s: %s", lyric_id, exc)
        return ""

    def _get_album_tracks(self, album_id: str) -> list[dict]:
        """Recupera la lista tracce di un album."""
        try:
            resp = self._session.get(
                _API_BASE,
                params={
                    "types":  "search",
                    "source": f"{_SOURCE}_album",
                    "name":   album_id,
                    "count":  100,
                    "pages":  1,
                },
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.debug("[migu] Album tracks fetch failed for id=%s: %s", album_id, exc)
        return []

    # ------------------------------------------------------------------
    # Conversion helper
    # ------------------------------------------------------------------

    def _item_to_metadata(self, item: dict, position: int = 1) -> TrackMetadata:
        """Converte un dizionario dell'API in un oggetto TrackMetadata."""
        track_id = str(item.get("id", ""))
        title    = item.get("name", "Unknown")

        raw_artists = item.get("artist", [])
        if isinstance(raw_artists, list):
            artist_str = ", ".join(
                a.get("name", "") if isinstance(a, dict) else str(a)
                for a in raw_artists
            ).strip(", ") or "Unknown"
        else:
            artist_str = str(raw_artists) or "Unknown"

        album  = item.get("album", "Unknown")
        pic_id = str(item.get("pic_id", ""))

        cover_url = self._get_pic_url(pic_id) if pic_id else ""

        return TrackMetadata(
            id           = f"migu_{track_id}",
            title        = title,
            artists      = artist_str,
            album        = album,
            album_artist = artist_str,
            duration_ms  = 0,
            cover_url    = cover_url,
            external_url = "",
            extra_info   = {
                "provider":     self.name,
                "raw_track_id": track_id,
                "pic_id":       pic_id,
                "lyric_id":     str(item.get("lyric_id", track_id)),
            },
        )

    # ------------------------------------------------------------------
    # get_url (Interfaccia per URL e Collezioni)
    # ------------------------------------------------------------------

    def get_url(self, url: str) -> tuple[str, list[TrackMetadata]]:
        """Interpreta URL o query e restituisce la collezione trovata."""
        match = re.search(r"(\d{5,})", url)

        if match and "_album" in url.lower():
            album_id = match.group(1)
            items = self._get_album_tracks(album_id)
            if items:
                tracks = [self._item_to_metadata(it, i + 1) for i, it in enumerate(items)]
                album_name = tracks[0].album if tracks else "Unknown Album"
                return album_name, tracks

        if match:
            track_id = match.group(1)
            items = self._search(track_id, count=1)
            if items:
                meta = self._item_to_metadata(items[0])
                return meta.title, [meta]

        query = url.strip()
        items = self._search(query, count=20)
        if not items:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                f"Nessun risultato trovato su Migu per: {query}",
                self.name,
            )
        tracks = [self._item_to_metadata(it, i + 1) for i, it in enumerate(items)]
        return f"Search: {query}", tracks

    # ------------------------------------------------------------------
    # download_track (Implementazione BaseProvider)
    # ------------------------------------------------------------------

    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            quality:             str              = "LOSSLESS",
            filename_format:     str              = "{title} - {artist}",
            position:            int              = 1,
            include_track_num:   bool             = False,
            use_album_track_num: bool             = False,
            first_artist_only:   bool             = False,
            allow_fallback:      bool             = True,
            embed_lyrics:        bool             = False,
            lyrics_providers:    list[str] | None = None,
            enrich_metadata:     bool             = False,
            enrich_providers:    list[str] | None = None,
            qobuz_token:         str | None       = None,
            is_album:            bool             = False,
            **kwargs:            Any,
    ) -> DownloadResult:

        try:
            extra        = metadata.extra_info or {}
            raw_track_id = extra.get("raw_track_id", "")

            # 1. Ricerca se non abbiamo l'ID di Migu
            if not raw_track_id:
                query = f"{metadata.title} {metadata.first_artist}".strip()
                logger.info("[migu] Cerco traccia: %s", query)
                items = self._search(query, count=5)
                if not items:
                    raise TrackNotFoundError(self.name, f"Traccia non trovata su Migu: {query}")
                raw_track_id = str(items[0].get("id", ""))
                if not raw_track_id:
                    raise TrackNotFoundError(self.name, "ID traccia vuoto dai risultati di Migu")
                extra = {
                    "raw_track_id": raw_track_id,
                    "pic_id":       str(items[0].get("pic_id", "")),
                    "lyric_id":     str(items[0].get("lyric_id", raw_track_id)),
                }

            # 2. Richiesta del flusso audio all'API con Fallback intelligente
            dl_url, extension, quality_label = self._get_stream(raw_track_id, quality)
            if not dl_url:
                raise SpotiflacError(
                    ErrorKind.UNAVAILABLE,
                    f"Nessun flusso lossless/hi-res disponibile su Migu per id={raw_track_id}",
                    self.name,
                )

            # 3. Ottenere la copertina
            cover_url = metadata.cover_url
            if not cover_url:
                pic_id = extra.get("pic_id", "")
                if pic_id:
                    cover_url = self._get_pic_url(pic_id)

            # 4. Costruzione del percorso finale in base all'estensione ritornata (.flac o .m4a)
            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num, first_artist_only,
                extension=extension,
            )

            if self._file_exists(dest):
                fmt = extension.replace(".", "")
                return DownloadResult.skipped_result(self.name, str(dest), fmt=fmt)

            # 5. Fetch asincrono dei tag da MusicBrainz
            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None

            print_source_banner("migu", _API_BASE, quality_label)

            # 6. Download
            logger.info("[migu] Downloading '%s' (id=%s, quality=%s)", metadata.title, raw_track_id, quality_label)
            self._http.stream_to_file(dl_url, str(dest), self._progress_cb)

            # 7. Validazione di integrità
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                if dest.exists():
                    os.remove(str(dest))
                return DownloadResult.fail(self.name, f"Validazione fallita: {err_msg}")

            # 8. Testi originali Migu
            lyric_id  = extra.get("lyric_id", raw_track_id)
            migu_lyrics: str | None = None
            if embed_lyrics and lyric_id:
                migu_lyrics = self._get_lyric(lyric_id) or None

            # 9. Completamento Tag MusicBrainz
            mb_tags: dict[str, str] = {}
            if mb_fetcher:
                res     = mb_fetcher.future.result()
                mb_tags = mb_result_to_tags(res)

            # 10. Embed definitivo dei metadati
            if cover_url and cover_url != metadata.cover_url:
                metadata = metadata.model_copy(update={"cover_url": cover_url})

            opts = EmbedOptions(
                first_artist_only  = first_artist_only,
                cover_url          = cover_url or metadata.cover_url,
                extra_tags         = mb_tags,
                embed_lyrics       = embed_lyrics,
                lyrics_providers   = lyrics_providers or [],
                enrich             = enrich_metadata,
                enrich_providers   = enrich_providers,
                enrich_qobuz_token = qobuz_token or "",
                is_album           = is_album,
            )
            embed_metadata(str(dest), metadata, opts, session=self._session)

            # Embedding fallback se i provider standard non trovano il testo, ma Migu sì
            if migu_lyrics and migu_lyrics.strip():
                try:
                    if extension == ".flac":
                        from mutagen.flac import FLAC as _FLAC
                        audio = _FLAC(str(dest))
                        if "LYRICS" not in audio:
                            audio["LYRICS"] = migu_lyrics
                            audio.save()
                            logger.debug("[migu] Testo Migu aggiunto (%d chars)", len(migu_lyrics))
                    elif extension == ".m4a":
                        from mutagen.mp4 import MP4
                        audio = MP4(str(dest))
                        if "\xa9lyr" not in audio:
                            audio["\xa9lyr"] = migu_lyrics
                            audio.save()
                            logger.debug("[migu] Testo Migu aggiunto su M4A")
                except Exception as exc:
                    logger.warning("[migu] Impossibile aggiungere il testo nativo: %s", exc)

            fmt = extension.replace(".", "")
            return DownloadResult.ok(self.name, str(dest), fmt=fmt)

        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] Errore imprevisto", self.name)
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
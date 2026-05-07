# apple_music_provider.py
from __future__ import annotations

import logging
import os
import time
import requests
import shutil
from pathlib import Path

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# Endpoints descritti in index.js per il proxy di download
_PROXY_DIRECT_URL = "https://api.zarz.moe/v1/dl/app2"
_PROXY_QUEUED_BASE = "https://api.zarz.moe/v1/dl/app"

class AppleMusicProvider(BaseProvider):
    name = "apple-music"

    def __init__(self, timeout_s: int = 30, proxy_api_key: str = "") -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = requests.Session()

        # Imposta gli header basati sul proxy descritto in index.js
        headers = {
            "User-Agent": _DEFAULT_UA,
            "Accept": "application/json"
        }
        if proxy_api_key:
            headers["Authorization"] = f"Bearer {proxy_api_key}"
            headers["X-API-Key"] = proxy_api_key

        self._session.headers.update(headers)

    def _normalize_codec(self, quality: str) -> str:
        """Mappa la qualità richiesta in codec compatibili con Apple Music."""
        q = quality.lower()
        if q in ["alac", "atmos", "ac3", "aac", "aac-legacy"]:
            return q
        return "alac"  # Default fallback come in index.js

    def _resolve_track_url(self, isrc: str) -> str | None:
        """
        Sfrutta l'API pubblica di iTunes per trovare l'URL della traccia
        Apple Music senza aver bisogno di scraping o token JWT complessi.
        """
        try:
            url = f"https://itunes.apple.com/lookup?isrc={isrc}"
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("resultCount", 0) > 0:
                # Restituisce il link ufficiale (es. https://music.apple.com/...)
                return data["results"][0].get("trackViewUrl")
        except Exception as e:
            logger.warning("[apple-music] Risoluzione URL tramite iTunes fallita per ISRC %s: %s", isrc, e)
        return None

    def _get_stream_url(self, track_url: str, codec: str) -> str | None:
        """
        Tenta prima il download diretto (app2). Se fallisce o non supportato,
        ripiega sul download asincrono/in coda (app).
        """
        # 1. Tentativo Diretto (App2)
        try:
            logger.debug("[apple-music] Tento risoluzione diretta (app2)...")
            resp = self._session.post(
                _PROXY_DIRECT_URL,
                json={"url": track_url, "codec": codec},
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") and data.get("stream_url"):
                return data["stream_url"]
        except Exception as e:
            logger.warning("[apple-music] Fallback ad app2 non riuscito: %s", e)

        # 2. Tentativo in coda (App)
        try:
            logger.debug("[apple-music] Tento risoluzione in coda (app)...")
            resp = self._session.post(
                f"{_PROXY_QUEUED_BASE}/download",
                json={"url": track_url, "codec": codec},
                timeout=15
            )
            resp.raise_for_status()
            job_data = resp.json()
            job_id = job_data.get("job_id")

            if not job_id:
                raise SpotiflacError("Nessun job_id restituito dal proxy di coda.")

            # Polling come descritto in index.js (downloadPollIntervalMs = 2500)
            max_wait_s = 60 * 60  # 60 minuti max
            deadline = time.time() + max_wait_s

            while time.time() < deadline:
                st_resp = self._session.get(f"{_PROXY_QUEUED_BASE}/status/{job_id}", timeout=15)
                st_resp.raise_for_status()
                st_data = st_resp.json()
                status = st_data.get("status", "").lower()

                if status == "completed":
                    return f"{_PROXY_QUEUED_BASE}/file/{job_id}"
                elif status == "failed":
                    raise SpotiflacError(f"Errore nel job di download Apple: {st_data.get('error')}")

                time.sleep(2.5)  # Poll interval 2.5s

            raise SpotiflacError("Timeout nell'attesa della traccia dal proxy.")
        except Exception as e:
            logger.error("[apple-music] Impossibile recuperare lo stream audio: %s", e)
            return None

    def _download_audio_file(self, stream_url: str, output_path: Path) -> bool:
        """Scarica fisicamente lo stream restituendo il progresso al core."""
        temp_path = str(output_path) + ".part"
        try:
            with self._session.get(stream_url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                received = 0

                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            received += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(received, total)

            # Rinomina al completamento
            shutil.move(temp_path, str(output_path))
            return True
        except Exception as e:
            logger.error("[apple-music] Errore di connessione durante il salvataggio: %s", e)
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            quality:             str              = "alac",
            filename_format:     str              = "{title} - {artist}",
            position:            int              = 1,
            include_track_num:   bool             = False,
            use_album_track_num: bool             = False,
            first_artist_only:   bool             = False,
            allow_fallback:      bool             = True,
            embed_lyrics:        bool             = False,
            lyrics_providers:    list[str] | None = None,
            lyrics_spotify_token:str              = "",
            enrich_metadata:     bool             = False,
            enrich_providers:    list[str] | None = None,
            is_album:            bool             = False,
            **kwargs,
    ) -> DownloadResult:
        if not metadata.isrc:
            return DownloadResult.fail(self.name, "Nessun ISRC fornito: essenziale per la risoluzione Apple Music.")

        try:
            # Le estensioni Apple Music (M4A) per i formati Lossless o AAC
            codec = self._normalize_codec(quality)
            ext = ".m4a"

            dest = self._build_output_path(
                metadata,
                output_dir,
                filename_format=filename_format,
                position=position,
                include_track_num=include_track_num,
                use_album_track_num=use_album_track_num,
                first_artist_only=first_artist_only,
                extension=ext
            )

            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest))

            # UI opzionale (ispirata a deezer.py)
            try:
                from ..core.ui import print_source_banner
                print_source_banner("Apple Music", codec.upper())
            except ImportError:
                pass

            # 1. Trova l'URL di Apple Music
            track_url = self._resolve_track_url(metadata.isrc)
            if not track_url:
                return DownloadResult.fail(self.name, "Impossibile trovare la traccia su Apple Music tramite ISRC.")

            logger.info("[apple-music] Traccia trovata: %s", track_url)

            # 2. Ottieni lo stream di Download dal Proxy (Zarz)
            stream_url = self._get_stream_url(track_url, codec)
            if not stream_url:
                return DownloadResult.fail(self.name, "Nessuno stream audio disponibile dal resolver API.")

            # 3. Effettua il Download
            success = self._download_audio_file(stream_url, dest)
            if not success or not os.path.exists(dest):
                return DownloadResult.fail(self.name, "Download del file M4A fallito.")

            # 4. Inserimento dei Metadati
            # MusicBrainz lookup asincrono
            from ..core.musicbrainz import AsyncMBFetch
            mb_fetcher = AsyncMBFetch(metadata.isrc)

            mb_tags: dict[str, str] = {}
            res = mb_fetcher.result()
            if res:
                mapping = {
                    "mbid_track": "MUSICBRAINZ_TRACKID",
                    "mbid_album": "MUSICBRAINZ_ALBUMID",
                    "mbid_artist": "MUSICBRAINZ_ARTISTID",
                }
                for mb_key, tag_name in mapping.items():
                    val = res.get(mb_key)
                    if val:
                        mb_tags[tag_name] = str(val)

            # Utilizzo del tagger di sistema di SpotiFLAC
            from ..core.tagger import embed_metadata as _embed
            _embed(
                str(dest), metadata,
                first_artist_only       = first_artist_only,
                cover_url               = metadata.cover_url,
                session                 = self._session,
                extra_tags              = mb_tags,
                embed_lyrics            = embed_lyrics,
                lyrics_providers        = lyrics_providers,
                lyrics_spotify_token    = lyrics_spotify_token,
                enrich                  = enrich_metadata,
                enrich_providers        = enrich_providers,
                is_album                = is_album,
            )

            return DownloadResult.ok(self.name, str(dest))

        except SpotiflacError as exc:
            logger.error("[%s] %s", self.name, exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[%s] Errore imprevisto", self.name)
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")
"""
extensions/provider.py — JSExtensionProvider

Adatta un'estensione JS al contratto BaseProvider di SpotiFLAC.

Flusso download via estensione:
  1. checkAvailability(isrc, title, artist, options)
       → { available, track_id } oppure { available: False }
  2. download(track_id, quality, output_path, onProgress)
       → { success, file_path, title, artist, ... }
  3. Converti il risultato in DownloadResult e applica i tag se assenti.

Flusso URL diretto (es. utente incolla un link SoundCloud):
  1. handleURL(url) → { type: "track"|"album", ... }
  2. download(...)

Il provider viene registrato in PROVIDER_REGISTRY con chiave "ext:{name}",
es. "ext:soundcloud", "ext:pandora".
"""
from __future__ import annotations

import asyncio
import logging
import contextlib
import queue
import threading
from pathlib import Path
from typing import Any

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError, ErrorKind
from ..providers.base import BaseProvider
from .manager import ExtensionManager, InstalledExtension
from .runtime import JSRuntime, ExtensionRuntimeError
from ..core.signed_session import SignedSessionClient, perform_signed_fetch, client_from_manifest

logger = logging.getLogger(__name__)


class JSExtensionProvider(BaseProvider):
    """
    Provider che delega download e metadati a un'estensione JS.

    Supporta i DOWNLOAD IN PARALLELO grazie a un Process Pool di JSRuntime.
    Invece di un singolo processo Node.js bloccante, crea fino a `max_runtimes`
    processi simultanei per scaricare le tracce alla stessa velocità dei provider nativi.
    """

    def __init__(
        self,
        ext_id:          str,
        settings:        dict | None = None,
        ext_dir:         str | None  = None,
        node_executable: str         = "node",
        timeout_s:       int         = 120,
    ) -> None:
        self._timeout_s       = timeout_s
        self._node_executable = node_executable
        self._progress_cb     = None
        self._stop_event      = None

        self._mgr = ExtensionManager(ext_dir=ext_dir)
        self._ext: InstalledExtension = self._load_extension(ext_id)
        self.name = f"ext:{self._ext.name}"

        # Merge settings
        self._settings = self._ext.default_settings()
        if settings:
            self._settings.update(settings)
        disk_settings = self._mgr.load_settings(ext_id)
        self._settings.update(disk_settings)
        if settings:
            self._settings.update(settings)

        # Configurazione signedSession
        self._signed_session: SignedSessionClient | None = None
        required = self._ext.manifest.get("requiredRuntimeFeatures", [])
        ss_config = self._ext.manifest.get("signedSession")
        if ss_config and any(f.startswith("signedSession") for f in required):
            self._signed_session = SignedSessionClient(
                base_url       = ss_config["baseUrl"],
                namespace      = ss_config["namespace"],
                app_version    = ss_config.get("appVersion", "1.0"),
                platform       = ss_config.get("platform", "extension"),
                scheme_label   = ss_config.get("schemeLabel", "SPOTIFLAC-HMAC-V1"),
                header_prefix  = ss_config.get("headerPrefix", "X-Sig-"),
                window_seconds = ss_config.get("timeWindowSeconds", 300),
                endpoints      = ss_config.get("endpoints"),
            )

        # --- RUNTIME POOL CONFIGURATION ---
        self._max_runtimes = 2
        self._runtimes_created = 0
        self._idle_runtimes = queue.Queue()
        self._all_runtimes = []
        self._runtime_lock = threading.Lock()

    # ─────────────────────── helpers ──────────────────────────

    def _load_extension(self, ext_id: str) -> InstalledExtension:
        ext = self._mgr.get_installed(ext_id)
        if ext is None:
            raise ValueError(
                f"Estensione '{ext_id}' non installata. "
                f"Usa ExtensionManager().install('{ext_id}') prima."
            )
        return ext

    async def _session_signed_fetch_handler(
        self, method: str, path: str, body: Any, headers: dict
    ) -> dict:
        ss_manifest = self._ext.manifest.get("signedSession")
        if not ss_manifest:
            return {"error": "signedSession manifest missing"}

        client = client_from_manifest(ss_manifest)
        try:
            return await perform_signed_fetch(client, method, path, body, headers)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    def _create_runtime(self) -> JSRuntime:
        """Crea una nuova istanza isolata del processo Node.js."""
        rt = JSRuntime(
            ext_path        = self._ext.index_js,
            settings        = self._settings,
            node_executable = self._node_executable,
            startup_timeout = 30.0,
            session_handler = (
                self._session_signed_fetch_handler
                if self._signed_session is not None else None
            ),
        )
        rt.start()
        return rt

    @contextlib.contextmanager
    def _acquire_runtime(self):
        """Pool manager: fornisce un runtime Node.js disponibile, creandolo se necessario."""
        rt = None
        try:
            # Prova a prendere un processo Node già avviato e libero
            rt = self._idle_runtimes.get_nowait()
        except queue.Empty:
            # Se sono tutti occupati, possiamo crearne uno nuovo?
            with self._runtime_lock:
                if self._runtimes_created < self._max_runtimes:
                    rt = self._create_runtime()
                    self._runtimes_created += 1
                    self._all_runtimes.append(rt)
        
        # Se siamo al limite massimo di processi, dobbiamo aspettare che se ne liberi uno
        if rt is None:
            try:
                rt = self._idle_runtimes.get(timeout=self._timeout_s)
            except queue.Empty:
                raise SpotiflacError(
                    kind=ErrorKind.NETWORK_ERROR,
                    message="Timeout: tutti i processi dell'estensione sono occupati.",
                    provider=self.name,
                )

        try:
            yield rt
        finally:
            # A fine lavoro (es. a download finito), rimetti il processo nel pool
            self._idle_runtimes.put(rt)

    def _call(self, method: str, *args, **kw) -> object:
        try:
            # Acquisiamo un "operaio" dal Pool e gli affidiamo il compito
            with self._acquire_runtime() as rt:
                return rt.call(method, *args, timeout=self._timeout_s, **kw)
        except ExtensionRuntimeError as e:
            raise SpotiflacError(
                kind     = ErrorKind.NETWORK_ERROR,
                message  = str(e),
                provider = self.name,
            ) from e

    def close(self) -> None:
        """Chiude brutalmente tutti i processi Node.js nel Pool."""
        with self._runtime_lock:
            for rt in self._all_runtimes:
                try:
                    rt.stop()
                except Exception:
                    pass
            self._all_runtimes.clear()
            self._runtimes_created = 0
            
            # Svuota la sala d'attesa
            while not self._idle_runtimes.empty():
                try:
                    self._idle_runtimes.get_nowait()
                except queue.Empty:
                    break
                    
        if self._signed_session is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._signed_session.aclose())
            except RuntimeError:
                asyncio.run(self._signed_session.aclose())

    def __enter__(self) -> "JSExtensionProvider":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ─────────────────────── API estesa ───────────────────────

    def check_availability(
        self,
        isrc:        str,
        track_name:  str,
        artist_name: str,
        options:     dict | None = None,
    ) -> dict:
        return self._call(
            "checkAvailability",
            isrc, track_name, artist_name,
            options or {},
        ) or {"available": False}

    def handle_url(self, url: str) -> dict:
        return self._call("handleUrl", url) or {}

    def get_track(self, track_id: str) -> dict:
        return self._call("getTrack", track_id) or {}

    def get_album(self, album_id: str) -> dict:
        return self._call("getAlbum", album_id) or {}

    def get_artist(self, artist_id: str) -> dict:
        return self._call("getArtist", artist_id) or {}

    def get_playlist(self, playlist_id: str) -> dict:
        return self._call("getPlaylist", playlist_id) or {}

    def search_tracks(self, query: str, options: dict | None = None) -> dict:
        return self._call("searchTracks", query, options or {}) or {}

    def custom_search(self, query: str, options: dict | None = None) -> dict:
        return self._call("customSearch", query, options or {}) or {}

    def enrich_track(self, track: dict, options: dict | None = None) -> dict:
        return self._call("enrichTrack", track, options or {}) or {}

    # ─────────────────────── BaseProvider (async, richiesto da 1.3.x) ─────

    async def download_track_async(
        self,
        metadata:             TrackMetadata,
        output_dir:           str,
        *,
        filename_format:      str          = "{title} - {artist}",
        position:             int          = 1,
        include_track_num:    bool         = False,
        use_album_track_num:  bool         = False,
        first_artist_only:    bool         = False,
        allow_fallback:       bool         = True,
        embed_lyrics:         bool         = False,
        lyrics_providers:     list | None  = None,
        enrich_metadata:      bool         = False,
        enrich_providers:     list | None  = None,
        is_album:             bool         = False,
        quality:              str          = "best",
        **kwargs,
    ) -> DownloadResult:
        try:
            return await self._do_download_async(
                metadata, output_dir,
                filename_format   = filename_format,
                position          = position,
                include_track_num = include_track_num,
                use_album_track_num = use_album_track_num,
                first_artist_only = first_artist_only,
                quality           = quality,
            )
        except SpotiflacError as e:
            return DownloadResult.fail(self.name, str(e))
        except Exception as e:
            logger.exception("[%s] Unexpected error", self.name)
            return DownloadResult.fail(self.name, f"Unexpected error: {e}")

    async def _do_download_async(
        self,
        metadata:            TrackMetadata,
        output_dir:          str,
        *,
        filename_format:     str,
        position:            int,
        include_track_num:   bool,
        use_album_track_num: bool,
        first_artist_only:   bool,
        quality:             str,
    ) -> DownloadResult:
        avail = await asyncio.to_thread(
            self.check_availability,
            isrc        = metadata.isrc,
            track_name  = metadata.title,
            artist_name = metadata.artists,
            options     = {
                "duration_ms": metadata.duration_ms,
                "spotify_id":  metadata.id,
            },
        )

        if not avail.get("available"):
            reason = avail.get("reason", "not found")
            return DownloadResult.fail(self.name, f"Track not available: {reason}")

        track_id = avail.get("track_id", "")
        if not track_id:
            return DownloadResult.fail(self.name, "checkAvailability returned no track_id")

        ext_hint = _quality_to_ext(quality)
        output_path = self._build_output_path(
            metadata            = metadata,
            output_dir          = output_dir,
            filename_format     = filename_format,
            position            = position,
            include_track_num   = include_track_num,
            use_album_track_num = use_album_track_num,
            first_artist_only   = first_artist_only,
            extension           = ext_hint,
        )

        if self._file_exists(output_path):
            return DownloadResult.skipped_result(
                self.name, str(output_path),
                fmt = _ext_to_fmt(output_path.suffix),
            )

        def _progress_adapter(fraction: float) -> None:
            if self._progress_cb is None:
                return
            try:
                current = int(max(0.0, min(1.0, fraction)) * 10_000)
                self._progress_cb(current, 10_000)
            except Exception:
                pass

        logger.info("[%s] Downloading '%s'", self.name, metadata.title)
        dl_result = await asyncio.to_thread(
            self._call,
            "download",
            track_id, quality, str(output_path), None,
            progress_cb = _progress_adapter,
        )

        if not dl_result or not dl_result.get("success"):
            err = (dl_result or {}).get("error_message", "download failed")
            return DownloadResult.fail(self.name, err)

        actual_path = dl_result.get("file_path") or str(output_path)
        fmt         = _ext_to_fmt(Path(actual_path).suffix)

        try:
            from ..core.tagger import embed_metadata_async, EmbedOptions
            await embed_metadata_async(
                Path(actual_path),
                metadata,
                EmbedOptions(
                    cover_url          = metadata.cover_url or "",
                    first_artist_only  = first_artist_only,
                ),
            )
        except Exception as e:
            logger.warning("[%s] Tagging failed (non-fatal): %s", self.name, e)

        return DownloadResult.ok(self.name, actual_path, fmt)

# ─────────────────────────────────────────────────────────────
#  Helpers & Factory
# ─────────────────────────────────────────────────────────────

def _quality_to_ext(quality: str) -> str:
    q = quality.lower()
    if "flac" in q:                return ".flac"
    if "mp3" in q:                 return ".mp3"
    if "aac" in q or "m4a" in q:  return ".m4a"
    if "opus" in q:                return ".opus"
    return ".flac"

def _ext_to_fmt(suffix: str) -> str:
    return {".flac": "flac", ".mp3": "mp3", ".m4a": "m4a"}.get(suffix.lower(), "flac")

def make_extension_provider(
    ext_id:          str,
    settings:        dict | None = None,
    ext_dir:         str | None  = None,
    node_executable: str         = "node",
    timeout_s:       int         = 120,
) -> JSExtensionProvider:
    return JSExtensionProvider(
        ext_id          = ext_id,
        settings        = settings,
        ext_dir         = ext_dir,
        node_executable = node_executable,
        timeout_s       = timeout_s,
    )
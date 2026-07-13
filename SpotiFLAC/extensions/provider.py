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
from pathlib import Path
from typing import Any

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError, ErrorKind
from ..providers.base import BaseProvider
from .manager import ExtensionManager, InstalledExtension
from .runtime import JSRuntime, ExtensionRuntimeError
from ..core.signed_session import SignedSessionClient, perform_signed_fetch

logger = logging.getLogger(__name__)


class JSExtensionProvider(BaseProvider):
    """
    Provider che delega download e metadati a un'estensione JS.

    Parametri:
        ext_id          – nome dell'estensione (es. "soundcloud")
        settings        – dict di configurazione (sovrascrive i default del manifest)
        ext_dir         – cartella estensioni (default: ~/.spotiflac/extensions)
        node_executable – path di Node.js (default: "node")
        timeout_s       – timeout per ogni operazione JS (default: 120)

    Se il manifest dell'estensione dichiara "requiredRuntimeFeatures" con
    "signedSession@1" (es. tidal-web, amazon, ecc.), viene automaticamente
    istanziato un SignedSessionClient dalla sezione "signedSession" del
    manifest e collegato a `session.signedFetch(...)` lato JS — l'estensione
    può quindi usare la sua logica originale (manifest parsing, fallback
    qualità, ecc.) invariata, senza bisogno di riscriverla in Python.
    """

    def __init__(
        self,
        ext_id:          str,
        settings:        dict | None = None,
        ext_dir:         str | None  = None,
        node_executable: str         = "node",
        timeout_s:       int         = 120,
    ) -> None:
        # Non chiamiamo super().__init__() con HttpClient perché l'HTTP lo gestisce Node,
        # ma dobbiamo comunque inizializzare gli attributi che BaseProvider si aspetta.
        self._timeout_s       = timeout_s
        self._node_executable = node_executable
        self._progress_cb     = None   # impostato da set_progress_callback()
        self._stop_event      = None   # impostato da set_stop_event()

        self._mgr = ExtensionManager(ext_dir=ext_dir)
        self._ext: InstalledExtension = self._load_extension(ext_id)
        self.name = f"ext:{self._ext.name}"

        # Merge settings: defaults manifest + settings utente
        self._settings = self._ext.default_settings()
        if settings:
            self._settings.update(settings)
        # Carica settings salvati su disco (priorità più alta dei default)
        disk_settings = self._mgr.load_settings(ext_id)
        self._settings.update(disk_settings)
        if settings:
            self._settings.update(settings)  # settings espliciti hanno massima priorità

        # signedSession: solo se il manifest lo dichiara (es. tidal-web,
        # amazon). Confermato via manifest reale (tidal-web/manifest.json):
        # {"namespace","baseUrl","appVersion","platform","schemeLabel",
        #  "headerPrefix","timeWindowSeconds","endpoints"}.
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

        self._runtime: JSRuntime | None = None
        self._runtime_lock = __import__("threading").Lock()

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
        """
        Collega session.signedFetch(...) lato JS a perform_signed_fetch()
        (già scritta e testata in signed_session.py): stessa forma di
        ritorno che il JS si aspetta ({statusCode, body, ok, headers,
        error, needsVerification, auth_url}).
        """
        assert self._signed_session is not None
        return await perform_signed_fetch(self._signed_session, method, path, body, headers)

    def _get_runtime(self) -> JSRuntime:
        """Ritorna il runtime attivo, avviandolo se necessario (lazy start)."""
        with self._runtime_lock:
            if self._runtime is None:
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
                self._runtime = rt
        return self._runtime

    def _call(self, method: str, *args, **kw) -> object:
        try:
            return self._get_runtime().call(method, *args, timeout=self._timeout_s, **kw)
        except ExtensionRuntimeError as e:
            raise SpotiflacError(
                kind     = ErrorKind.NETWORK_ERROR,
                message  = str(e),
                provider = self.name,
            ) from e

    def close(self) -> None:
        """Ferma il processo Node.js e l'eventuale SignedSessionClient. Chiamare esplicitamente o usare come context manager."""
        with self._runtime_lock:
            if self._runtime:
                self._runtime.stop()
                self._runtime = None
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
        """
        Chiama checkAvailability sull'estensione JS.
        Ritorna il dict grezzo: { available: bool, track_id?: str, ... }
        """
        return self._call(
            "checkAvailability",
            isrc, track_name, artist_name,
            options or {},
        ) or {"available": False}

    def handle_url(self, url: str) -> dict:
        """
        Chiama handleUrl sull'estensione JS (il nome esposto da registerExtension
        è sempre 'handleUrl', minuscolo, in tutte le estensioni ufficiali).
        Ritorna il dict grezzo con type, tracks, metadata, ecc.
        """
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
        """Ricerca full-text tracce (se l'estensione lo supporta)."""
        return self._call("searchTracks", query, options or {}) or {}

    def custom_search(self, query: str, options: dict | None = None) -> dict:
        return self._call("customSearch", query, options or {}) or {}

    def enrich_track(self, track: dict, options: dict | None = None) -> dict:
        """Arricchisce metadati di una traccia già nota (ISRC, label, ecc.)."""
        return self._call("enrichTrack", track, options or {}) or {}

    # ─────────────────────── BaseProvider ─────────────────────

    def download_track(
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
        """
        Implementazione BaseProvider.download_track().

        Strategia:
          1. Se il manifest ha checkAvailability, cercalo per ISRC.
          2. Se trovato, scarica con download().
          3. Applica tag al file risultante se non già presenti.
        """
        try:
            return self._do_download(
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

    def _do_download(
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
        # 1. Trova il track_id via checkAvailability (ISRC-based)
        avail = self.check_availability(
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

        # 2. Costruisce il path di output
        #    Usiamo un'estensione temporanea .tmp — l'estensione JS la sovrascriverà
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

        # Salta se già esistente
        if self._file_exists(output_path):
            return DownloadResult.skipped_result(
                self.name, str(output_path),
                fmt = _ext_to_fmt(output_path.suffix),
            )

        # 3. Chiama download() sull'estensione JS.
        #    Le estensioni JS riportano il progresso come float 0..1 (percentuale),
        #    mentre BaseProvider._progress_cb si aspetta (current_bytes, total_bytes).
        #    Usiamo una scala fissa 0..10000 come proxy per non perdere precisione.
        def _progress_adapter(fraction: float) -> None:
            if self._progress_cb is None:
                return
            try:
                current = int(max(0.0, min(1.0, fraction)) * 10_000)
                self._progress_cb(current, 10_000)
            except Exception:
                pass  # il progress reporting non deve mai far fallire il download

        logger.info("[%s] Downloading '%s'", self.name, metadata.title)
        dl_result = self._call(
            "download",
            track_id, quality, str(output_path), None,
            progress_cb = _progress_adapter,
        )

        if not dl_result or not dl_result.get("success"):
            err = (dl_result or {}).get("error_message", "download failed")
            return DownloadResult.fail(self.name, err)

        actual_path = dl_result.get("file_path") or str(output_path)
        fmt         = _ext_to_fmt(Path(actual_path).suffix)

        # 4. Applica tag se l'estensione non li ha già applicati
        #    (usiamo i metadati Spotify che abbiamo già — più completi)
        try:
            from ..core.tagger import embed_metadata, EmbedOptions
            embed_metadata(
                Path(actual_path),
                metadata,
                EmbedOptions(
                    embed_cover        = bool(metadata.cover_url),
                    first_artist_only  = first_artist_only,
                ),
            )
        except Exception as e:
            logger.warning("[%s] Tagging failed (non-fatal): %s", self.name, e)

        return DownloadResult.ok(self.name, actual_path, fmt)


# ─────────────────────────────────────────────────────────────
#  Helpers
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


# ─────────────────────────────────────────────────────────────
#  Factory function
# ─────────────────────────────────────────────────────────────

def make_extension_provider(
    ext_id:          str,
    settings:        dict | None = None,
    ext_dir:         str | None  = None,
    node_executable: str         = "node",
    timeout_s:       int         = 120,
) -> JSExtensionProvider:
    """
    Crea un JSExtensionProvider per l'estensione specificata.
    Conveniente per uso programmatico.
    """
    return JSExtensionProvider(
        ext_id          = ext_id,
        settings        = settings,
        ext_dir         = ext_dir,
        node_executable = node_executable,
        timeout_s       = timeout_s,
    )
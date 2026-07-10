"""
extensions/provider.py — JSExtensionProvider

Adapts a JS extension to SpotiFLAC's BaseProvider contract.

Download flow via extension:
  1. checkAvailability(isrc, title, artist, options)
       → { available, track_id } or { available: False }
  2. download(track_id, quality, output_path, onProgress)
       → { success, file_path, title, artist, ... }
  3. Convert result to DownloadResult and apply tags if missing.

Direct URL flow (e.g. user pastes a SoundCloud link):
  1. handleURL(url) → { type: "track"|"album", ... }
  2. download(...)

The provider is registered in PROVIDER_REGISTRY with key "ext:{name}",
e.g. "ext:soundcloud", "ext:pandora".
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError, ErrorKind
from ..providers.base import BaseProvider
from .manager import ExtensionManager, InstalledExtension
from .runtime import JSRuntime, ExtensionRuntimeError

logger = logging.getLogger(__name__)


class JSExtensionProvider(BaseProvider):
    """
    Provider that delegates download and metadata to a JS extension.

    Parameters:
        ext_id          – extension name (e.g. "soundcloud")
        settings        – configuration dict (overrides manifest defaults)
        ext_dir         – extensions folder (default: ~/.spotiflac/extensions)
        node_executable – path to Node.js (default: "node")
        timeout_s       – timeout for each JS operation (default: 120)
    """

    def __init__(
        self,
        ext_id:          str,
        settings:        dict | None = None,
        ext_dir:         str | None  = None,
        node_executable: str         = "node",
        timeout_s:       int         = 120,
    ) -> None:
        # We don't call super().__init__() with HttpClient because Node.js handles HTTP,
        # but we still need to initialize the attributes that BaseProvider expects.
        self._timeout_s       = timeout_s
        self._node_executable = node_executable
        self._progress_cb     = None   # set by set_progress_callback()
        self._stop_event      = None   # set by set_stop_event()

        self._mgr = ExtensionManager(ext_dir=ext_dir)
        self._ext: InstalledExtension = self._load_extension(ext_id)
        self.name = f"ext:{self._ext.name}"

        # Merge settings: manifest defaults + user settings
        self._settings = self._ext.default_settings()
        if settings:
            self._settings.update(settings)
        # Load settings saved to disk (higher priority than defaults)
        disk_settings = self._mgr.load_settings(ext_id)
        self._settings.update(disk_settings)
        if settings:
            self._settings.update(settings)  # explicit settings have highest priority

        self._runtime: JSRuntime | None = None
        self._runtime_lock = __import__("threading").Lock()

    # ─────────────────────── helpers ──────────────────────────

    def _load_extension(self, ext_id: str) -> InstalledExtension:
        ext = self._mgr.get_installed(ext_id)
        if ext is None:
            raise ValueError(
                f"Extension '{ext_id}' not installed. "
                f"Use ExtensionManager().install('{ext_id}') first."
            )
        return ext

    def _get_runtime(self) -> JSRuntime:
        """Returns the active runtime, starting it if necessary (lazy start)."""
        with self._runtime_lock:
            if self._runtime is None:
                rt = JSRuntime(
                    ext_path        = self._ext.index_js,
                    settings        = self._settings,
                    node_executable = self._node_executable,
                    startup_timeout = 30.0,
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
        """Stops the Node.js process. Call explicitly or use as context manager."""
        with self._runtime_lock:
            if self._runtime:
                self._runtime.stop()
                self._runtime = None

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
        Calls checkAvailability on the JS extension.
        Returns the raw dict: { available: bool, track_id?: str, ... }
        """
        return self._call(
            "checkAvailability",
            isrc, track_name, artist_name,
            options or {},
        ) or {"available": False}

    def handle_url(self, url: str) -> dict:
        """
        Calls handleUrl on the JS extension (the name exposed by registerExtension
        is always 'handleUrl', lowercase, in all official extensions).
        Returns the raw dict with type, tracks, metadata, etc.
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
        """Full-text search for tracks (if the extension supports it)."""
        return self._call("searchTracks", query, options or {}) or {}

    def custom_search(self, query: str, options: dict | None = None) -> dict:
        return self._call("customSearch", query, options or {}) or {}

    def enrich_track(self, track: dict, options: dict | None = None) -> dict:
        """Enriches metadata of an already-known track (ISRC, label, etc.)."""
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
        BaseProvider.download_track() implementation.

        Strategy:
          1. If the manifest has checkAvailability, search by ISRC.
          2. If found, download with download().
          3. Apply tags to the resulting file if not already present.
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
        """Async wrapper for the sync extension download implementation."""
        loop = asyncio.get_running_loop()
        func = lambda: self.download_track(
            metadata,
            output_dir,
            filename_format=filename_format,
            position=position,
            include_track_num=include_track_num,
            use_album_track_num=use_album_track_num,
            first_artist_only=first_artist_only,
            allow_fallback=allow_fallback,
            embed_lyrics=embed_lyrics,
            lyrics_providers=lyrics_providers,
            enrich_metadata=enrich_metadata,
            enrich_providers=enrich_providers,
            is_album=is_album,
            quality=quality,
            **kwargs,
        )
        return await loop.run_in_executor(None, func)

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
        # 1. Find track_id via checkAvailability (ISRC-based)
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

        # 2. Build output path
        #    Use temporary .tmp extension — the JS extension will overwrite it
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

        # Skip if already exists
        if self._file_exists(output_path):
            return DownloadResult.skipped_result(
                self.name, str(output_path),
                fmt = _ext_to_fmt(output_path.suffix),
            )

        # 3. Call download() on the JS extension.
        #    JS extensions report progress as float 0..1 (percentage),
        #    while BaseProvider._progress_cb expects (current_bytes, total_bytes).
        #    Use fixed scale 0..10000 as proxy to avoid losing precision.
        def _progress_adapter(fraction: float) -> None:
            if self._progress_cb is None:
                return
            try:
                current = int(max(0.0, min(1.0, fraction)) * 10_000)
                self._progress_cb(current, 10_000)
            except Exception:
                pass  # progress reporting must never fail the download

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
    Creates a JSExtensionProvider for the specified extension.
    Convenient for programmatic use.
    """
    return JSExtensionProvider(
        ext_id          = ext_id,
        settings        = settings,
        ext_dir         = ext_dir,
        node_executable = node_executable,
        timeout_s       = timeout_s,
    )

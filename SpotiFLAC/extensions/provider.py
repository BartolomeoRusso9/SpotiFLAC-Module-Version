"""
extensions/provider.py — JSExtensionProvider

Adapts a JavaScript extension to the SpotiFLAC BaseProvider contract.

Download flow via extension:
  1. checkAvailability(isrc, title, artist, options)
       → { available, track_id } or { available: False }
  2. download(track_id, quality, output_path, onProgress)
       → { success, file_path, title, artist, ... }
  3. Convert the result to DownloadResult and apply tags if absent.

Direct URL flow (e.g. user pastes a SoundCloud link):
  1. handleURL(url) → { type: "track"|"album", ... }
  2. download(...)

The provider is registered in PROVIDER_REGISTRY with key "ext:{name}",
e.g. "ext:soundcloud", "ext:pandora".
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
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
    Provider that delegates downloads and metadata to a JavaScript extension.

    Supports PARALLEL DOWNLOADS thanks to a Process Pool of JSRuntime.
    Instead of a single blocking Node.js process, creates up to `max_runtimes`
    simultaneous processes to download tracks at the same speed as native providers.
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

        # signedSession configuration
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

    # ─────────────────────── BaseProvider overrides ───────────

    def set_progress_callback(self, cb) -> None:
        """Salva il callback per l'avanzamento fornito dal Downloader."""
        self._progress_cb = cb

    def set_stop_event_async(self, event: asyncio.Event) -> None:
        """Salva l'evento di stop (timeout/cancellazione) fornito dal Downloader."""
        self._stop_event = event

    def set_stop_event(self, event) -> None:
        """Retrocompatibilità sincrona."""
        self._stop_event = event

    # ─────────────────────── helpers ──────────────────────────

    def _load_extension(self, ext_id: str) -> InstalledExtension:
        ext = self._mgr.get_installed(ext_id)
        if ext is None:
            raise ValueError(
                f"Extension '{ext_id}' not installed. "
                f"Use ExtensionManager().install('{ext_id}') first."
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
        """Creates a new isolated instance of the Node.js process."""
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
        """Pool manager: provides an available Node.js runtime, creating it if necessary."""
        rt = None
        try:
            # Try to get an already-started and free Node process
            rt = self._idle_runtimes.get_nowait()
        except queue.Empty:
            # If all are busy, can we create a new one?
            with self._runtime_lock:
                if self._runtimes_created < self._max_runtimes:
                    rt = self._create_runtime()
                    self._runtimes_created += 1
                    self._all_runtimes.append(rt)

        # If we're at the max process limit, we need to wait for one to free up
        if rt is None:
            try:
                rt = self._idle_runtimes.get(timeout=self._timeout_s)
            except queue.Empty:
                raise SpotiflacError(
                    kind=ErrorKind.NETWORK_ERROR,
                    message="Timeout: all extension processes are busy.",
                    provider=self.name,
                )

        try:
            yield rt
        finally:
            # After work is done (e.g. download finished), put the process back in the pool
            self._idle_runtimes.put(rt)

    def _call(self, method: str, *args, **kw) -> object:
        try:
            # Acquire a "worker" from the Pool and assign it the task
            with self._acquire_runtime() as rt:
                return rt.call(method, *args, timeout=self._timeout_s, **kw)
        except ExtensionRuntimeError as e:
            raise SpotiflacError(
                kind     = ErrorKind.NETWORK_ERROR,
                message  = str(e),
                provider = self.name,
            ) from e

    def close(self) -> None:
        """Forcefully closes all Node.js processes in the Pool."""
        with self._runtime_lock:
            for rt in self._all_runtimes:
                try:
                    rt.stop()
                except Exception:
                    pass
            self._all_runtimes.clear()
            self._runtimes_created = 0

            # Empty the waiting room
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
        position:              int          = 1,
        include_track_num:    bool         = False,
        use_album_track_num:  bool         = False,
        first_artist_only:    bool         = False,
        allow_fallback:       bool         = True,
        embed_lyrics:         bool         = False,
        lyrics_providers:     list | None  = None,
        enrich_metadata:      bool         = False,
        enrich_providers:     list | None  = None,
        qobuz_token:          str | None   = None,
        is_album:             bool         = False,
        quality:              str          = "best",
        **kwargs,
    ) -> DownloadResult:
        try:
            return await self._do_download_async(
                metadata, output_dir,
                filename_format      = filename_format,
                position             = position,
                include_track_num    = include_track_num,
                use_album_track_num  = use_album_track_num,
                first_artist_only    = first_artist_only,
                quality              = quality,
                allow_fallback       = allow_fallback,
                embed_lyrics         = embed_lyrics,
                lyrics_providers     = lyrics_providers,
                enrich_metadata      = enrich_metadata,
                enrich_providers     = enrich_providers,
                qobuz_token          = qobuz_token,
                is_album             = is_album,
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
        allow_fallback:      bool         = True,
        embed_lyrics:        bool         = False,
        lyrics_providers:    list | None  = None,
        enrich_metadata:     bool         = False,
        enrich_providers:    list | None  = None,
        qobuz_token:         str | None   = None,
        is_album:            bool         = False,
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
                result = self._progress_cb(current, 10_000)
                # If the callback is async and returns a coroutine, we can't await it here
                # (we're in a sync context via to_thread), so we need to close it to prevent warnings
                if asyncio.iscoroutine(result):
                    result.close()
            except Exception:
                pass

        logger.info("[%s] Downloading '%s'", self.name, metadata.title)

        # ── Progress fallback via disk polling ──────────────────────────
        # Copre il caso in cui l'estensione bypassi global.file.download
        # (es. scrivendo segmenti manualmente via file.writeBytes) e quindi
        # non generi mai eventi "progress" reali dal bridge. Se invece gli
        # eventi reali arrivano (ora emessi da nodeFileDownload in
        # _bridge.js), questo fallback è semplicemente ridondante e innocuo.
        poll_stop = asyncio.Event()
        poll_task = asyncio.create_task(
            self._poll_file_progress_async(output_path, poll_stop)
        )

        try:
            dl_result = await asyncio.to_thread(
                self._call,
                "download",
                track_id, quality, str(output_path), None,
                progress_cb = _progress_adapter,
            )
        finally:
            poll_stop.set()
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

        if not dl_result or not dl_result.get("success"):
            err = (dl_result or {}).get("error_message", "download failed")
            return DownloadResult.fail(self.name, err)

        # ── Riassembla eventuali segmenti (es. Tidal DASH via estensione) ──
        actual_path = await self._finalize_segments_async(dl_result, output_path)
        if actual_path is None:
            return DownloadResult.fail(
                self.name, "Download returned no usable audio (segments unmergeable)"
            )

        fmt = _ext_to_fmt(Path(actual_path).suffix)

        # ── MusicBrainz, come nei provider nativi (tidal.py, qobuz.py, ecc.) ──
        mb_tags: dict[str, str] = {}
        if enrich_metadata and metadata.isrc:
            try:
                from ..core.musicbrainz import fetch_mb_metadata_async, mb_result_to_tags
                from ..core.isrc_utils import normalize_isrc

                isrc_clean = normalize_isrc(metadata.isrc)
                if isrc_clean:
                    mb_data = await fetch_mb_metadata_async(isrc_clean)
                    mb_tags = mb_result_to_tags(mb_data)
            except Exception as e:
                logger.debug("[%s] MusicBrainz lookup failed (non-fatal): %s", self.name, e)

        try:
            from ..core.tagger import embed_metadata_async, EmbedOptions
            from ..core.download_validation import validate_downloaded_track_async

            expected_s = metadata.duration_ms // 1000
            valid, err_msg = await validate_downloaded_track_async(actual_path, expected_s)
            if not valid:
                logger.warning("[%s] Validation failed: %s", self.name, err_msg)
                return DownloadResult.fail(self.name, f"Validation failed: {err_msg}")

            await embed_metadata_async(
                Path(actual_path),
                metadata,
                EmbedOptions(
                    cover_url          = metadata.cover_url or "",
                    first_artist_only  = first_artist_only,
                    embed_lyrics       = embed_lyrics,
                    lyrics_providers   = lyrics_providers or [],
                    enrich             = enrich_metadata,
                    enrich_providers   = enrich_providers,
                    enrich_qobuz_token = qobuz_token or "",
                    is_album           = is_album,
                    extra_tags         = mb_tags,
                ),
            )
        except Exception as e:
            logger.warning("[%s] Tagging failed (non-fatal): %s", self.name, e)

        return DownloadResult.ok(self.name, actual_path, fmt)

    # ─────────────────────── Segment reassembly ────────────────────────

    async def _finalize_segments_async(
        self, dl_result: dict, output_path: Path
    ) -> str | None:
        """
        Alcune estensioni (es. tidal-web, che scarica via DASH/fMP4) possono
        restituire i segmenti scaricati invece di un unico file audio pronto.
        Contratto atteso, in ordine di priorità:

          1. dl_result["file_path"] esiste ed è un file valido e non vuoto
             → nessun lavoro extra, comportamento originale.
          2. dl_result["segments"] è una lista ordinata di path assoluti
             (init segment incluso, se presente) → li concateniamo come
             byte grezzi in un file temporaneo e poi rimuxiamo con ffmpeg
             (stesso schema di TidalProvider._download_from_manifest_async
             + _mux_audio_async, generalizzato per qualsiasi estensione).
          3. Fallback difensivo: se né 1 né 2 valgono, ma esistono file
             residui accanto a output_path che matchano il pattern
             "<stem>.partNN" o "<stem>.segNN" lasciati dall'estensione,
             li ordiniamo e li trattiamo come (2).

        Returns il path finale (str) del file audio pronto per il tagging,
        o None se non è stato possibile ricostruire un file valido.
        """
        file_path = dl_result.get("file_path")
        if file_path and Path(file_path).exists() and Path(file_path).stat().st_size > 0:
            return file_path

        segments: list[str] = dl_result.get("segments") or []

        if not segments:
            parent = output_path.parent
            stem = output_path.stem
            candidates = sorted(
                parent.glob(f"{stem}.part*"),
                key=lambda p: p.name,
            ) or sorted(
                parent.glob(f"{stem}.seg*"),
                key=lambda p: p.name,
            )
            segments = [str(p) for p in candidates if p.exists() and p.stat().st_size > 0]

        if not segments:
            return None

        logger.info(
            "[%s] Reassembling %d segment(s) into a single stream…",
            self.name, len(segments),
        )

        raw_concat = output_path.with_suffix(".raw.tmp")
        try:
            with open(raw_concat, "wb") as out_f:
                for seg_path in segments:
                    with open(seg_path, "rb") as seg_f:
                        out_f.write(seg_f.read())
        except Exception as e:
            logger.error("[%s] Segment concatenation failed: %s", self.name, e)
            if raw_concat.exists():
                raw_concat.unlink(missing_ok=True)
            return None

        codec = "flac"
        try:
            rc, stdout, stderr = await self._run_ffprobe(
                "ffprobe", "-v", "quiet",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(raw_concat),
            )
            if rc == 0 and stdout.strip():
                codec = stdout.strip().lower()
            else:
                logger.warning(
                    "[%s] ffprobe failed on reassembled stream, guessing codec: %s",
                    self.name, stderr.strip()[:150],
                )
        except Exception:
            logger.warning("[%s] ffprobe failed to detect codec after reassembly", self.name)

        is_lossy = codec not in ("flac", "alac")
        final_dest = (
            output_path.with_suffix(".m4a") if is_lossy else output_path.with_suffix(".flac")
        )

        cmd = ["ffmpeg", "-y", "-i", str(raw_concat), "-vn"]
        cmd.extend(["-c:a", "copy"] if is_lossy else ["-c:a", "flac"])
        cmd.append(str(final_dest))

        try:
            rc, stdout, stderr = await self._run_ffmpeg(*cmd)
        finally:
            if raw_concat.exists():
                raw_concat.unlink(missing_ok=True)
            for seg_path in segments:
                try:
                    Path(seg_path).unlink(missing_ok=True)
                except Exception:
                    pass

        if rc != 0:
            logger.error("[%s] ffmpeg mux failed: %s", self.name, stderr[:300])
            if final_dest.exists():
                final_dest.unlink(missing_ok=True)
            return None

        logger.info("[%s] Segments merged into: %s", self.name, final_dest.name)
        return str(final_dest)

    # ─────────────────────── Progress polling fallback ─────────────────

    async def _poll_file_progress_async(
        self, output_path: Path, stop_event: asyncio.Event
    ) -> None:
        """
        Osserva la crescita di output_path (o dei suoi varianti temporanei
        più comuni: .part, .tmp, .download) e alimenta self._progress_cb
        con una stima percentuale quando nessun evento "progress" reale
        arriva dal bridge JS (es. estensioni che scrivono i propri file
        senza passare da global.file.download).
        """
        if self._progress_cb is None:
            return

        candidates = [
            output_path,
            output_path.with_suffix(output_path.suffix + ".part"),
            output_path.with_suffix(output_path.suffix + ".tmp"),
            output_path.with_suffix(output_path.suffix + ".download"),
        ]

        last_size = 0
        elapsed_polls = 0

        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.3)
                elapsed_polls += 1

                size = 0
                for cand in candidates:
                    try:
                        if cand.exists():
                            size = max(size, cand.stat().st_size)
                    except OSError:
                        continue

                if size <= 0:
                    continue

                fraction = min(0.97, 1 - (1 / (1 + elapsed_polls * 0.15)))

                if size != last_size:
                    last_size = size
                    try:
                        current = int(fraction * 10_000)
                        result = self._progress_cb(current, 10_000)
                        # If the callback is async, await it
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass


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
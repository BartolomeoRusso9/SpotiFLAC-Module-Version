"""extensions/provider.py — JSExtensionProvider.

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
import time
from pathlib import Path
from typing import Any

from typing_extensions import Self

from SpotiFLAC.core.base import BaseProvider
from SpotiFLAC.core.errors import ErrorKind, SpotiflacError
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.core.output_lock import output_path_lock
from SpotiFLAC.core.text_match import track_identity_mismatch
from SpotiFLAC.core.signed_session_mobile import SignedSessionClient

from .manager import ExtensionManager, InstalledExtension
from .runtime import ExtensionRuntimeError, JSRuntime
from .runtime_features import signed_fetch, signed_session_client

logger = logging.getLogger(__name__)


def _human_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class _TransferProgress:
    """What is actually known about one download's byte counts.

    Two things report on the same transfer — the bridge's `progress`
    messages and the disk poller — and they know different amounts. The
    bridge has the Content-Length when the extension streams through
    `global.file.download`; the poller only ever has the size on disk. This
    holds whichever is better, so the progress bar is fed real bytes and the
    poller stops guessing the moment the bridge starts reporting.
    """

    __slots__ = ("bridge_reporting", "bytes_on_disk", "total_bytes")

    def __init__(self) -> None:
        self.bytes_on_disk = 0
        self.total_bytes = 0
        #: The bridge has sent at least one event carrying real byte counts.
        self.bridge_reporting = False

    def note(
        self, received: int, total: int | None, *, from_bridge: bool = False
    ) -> None:
        """Record a report. `from_bridge` marks it as the bridge's own bytes.

        The flag is separate from `total` because a chunked response has no
        Content-Length: the bridge still knows exactly how many bytes it has
        received, and that it is reporting at all is what tells the disk
        poller to stand down. Tying the two together let a total-less
        transfer be overwritten by the poller's guesses.
        """
        self.bytes_on_disk = max(self.bytes_on_disk, int(received or 0))
        if total:
            self.total_bytes = int(total)
        if from_bridge or total:
            self.bridge_reporting = True

    def estimate_total(self, fraction: float) -> int:
        """The full size implied by `fraction` of the bytes written so far.

        Only useful for an extension that reports a fraction without a
        Content-Length. Below 1% the division amplifies noise into a wildly
        wrong total, and a wrong total is worse than none: it draws a bar
        that fills and then keeps going. Unknown (0) is the honest answer
        there, and it renders as an indeterminate bar.
        """
        if self.total_bytes:
            return self.total_bytes
        if fraction >= 0.01 and self.bytes_on_disk > 0:
            return int(self.bytes_on_disk / min(1.0, fraction))
        return 0


class JSExtensionProvider(BaseProvider):
    """Provider that delegates downloads and metadata to a JavaScript extension.

    Supports PARALLEL DOWNLOADS thanks to a Process Pool of JSRuntime.
    Instead of a single blocking Node.js process, creates up to `max_runtimes`
    simultaneous processes to download tracks at the same speed as native providers.
    """

    def __init__(
        self,
        ext_id: str,
        settings: dict | None = None,
        ext_dir: str | None = None,
        node_executable: str = "node",
        timeout_s: int = 180,
    ) -> None:
        super().__init__(timeout_s=timeout_s)

        self._timeout_s = timeout_s
        self._node_executable = node_executable
        self._progress_cb = None
        self._stop_event = None

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
        self._signed_session = signed_session_client(self._ext.manifest)

        # --- RUNTIME POOL CONFIGURATION ---
        self._max_runtimes = 2
        self._runtimes_created = 0
        self._idle_runtimes = queue.Queue()
        self._all_runtimes = []
        self._runtime_lock = threading.Lock()
        self._active_calls = 0
        self._active_calls_condition = threading.Condition(self._runtime_lock)

    # ─────────────────────── BaseProvider overrides ───────────

    def set_progress_callback(self, cb) -> None:
        """Saves the progress callback provided by the Downloader."""
        self._progress_cb = cb

    def set_stop_event_async(self, event: asyncio.Event) -> None:
        """Saves the stop event (timeout/cancellation) provided by the Downloader."""
        self._stop_event = event

    def set_stop_event(self, event) -> None:
        """Synchronous backward compatibility."""
        self._stop_event = event

    # ─────────────────────── helpers ──────────────────────────

    def _load_extension(self, ext_id: str) -> InstalledExtension:
        ext = self._mgr.get_installed(ext_id)
        if ext is None:
            msg = (
                f"Extension '{ext_id}' not installed. "
                f"Use ExtensionManager().install('{ext_id}') first."
            )
            raise ValueError(
                msg,
            )
        return ext

    async def _session_signed_fetch_handler(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict,
    ) -> dict:
        return await signed_fetch(self._ext.manifest, method, path, body, headers)

    def _create_runtime(self) -> JSRuntime:
        """Creates a new isolated instance of the Node.js process."""
        rt = JSRuntime(
            ext_path=self._ext.index_js,
            settings=self._settings,
            node_executable=self._node_executable,
            startup_timeout=30.0,
            session_handler=(
                self._session_signed_fetch_handler
                if self._signed_session is not None
                else None
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
        with self._active_calls_condition:
            self._active_calls += 1
        try:
            # Acquire a "worker" from the Pool and assign it the task
            with self._acquire_runtime() as rt:
                return rt.call(method, *args, timeout=self._timeout_s, **kw)
        except ExtensionRuntimeError as e:
            raise SpotiflacError(
                kind=ErrorKind.NETWORK_ERROR,
                message=str(e),
                provider=self.name,
            ) from e
        finally:
            with self._active_calls_condition:
                self._active_calls -= 1
                self._active_calls_condition.notify_all()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Waits until timed-out background extension calls have returned."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._active_calls_condition:
            while self._active_calls:
                if deadline is None:
                    self._active_calls_condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_calls_condition.wait(remaining)
            return True

    async def wait_for_idle_async(self, timeout: float | None = None) -> bool:
        return await asyncio.to_thread(self.wait_for_idle, timeout)

    def close(self) -> None:
        """Forcefully closes all Node.js processes in the Pool."""
        self.wait_for_idle(5.0)
        with self._runtime_lock:
            for rt in self._all_runtimes:
                with contextlib.suppress(Exception):
                    rt.stop()
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ─────────────────────── API estesa ───────────────────────

    def check_availability(
        self,
        isrc: str,
        track_name: str,
        artist_name: str,
        options: dict | None = None,
    ) -> dict:
        return self._call(
            "checkAvailability",
            isrc,
            track_name,
            artist_name,
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
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format: str = "{title} - {artist}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        artist_separator: str | None = None,
        allow_fallback: bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list | None = None,
        apple_lyrics_word_by_word: bool = True,
        enrich_metadata: bool = False,
        enrich_providers: list | None = None,
        qobuz_token: str | None = None,
        is_album: bool = False,
        quality: str = "best",
        **kwargs,
    ) -> DownloadResult:
        try:
            return await self._do_download_async(
                metadata,
                output_dir,
                filename_format=filename_format,
                position=position,
                include_track_num=include_track_num,
                use_album_track_num=use_album_track_num,
                first_artist_only=first_artist_only,
                artist_separator=artist_separator,
                quality=quality,
                allow_fallback=allow_fallback,
                embed_lyrics=embed_lyrics,
                lyrics_providers=lyrics_providers,
                apple_lyrics_word_by_word=apple_lyrics_word_by_word,
                enrich_metadata=enrich_metadata,
                enrich_providers=enrich_providers,
                qobuz_token=qobuz_token,
                is_album=is_album,
            )
        except SpotiflacError as e:
            return DownloadResult.fail(self.name, str(e))
        except Exception as e:
            logger.exception("[%s] Unexpected error", self.name)
            return DownloadResult.fail(self.name, f"Unexpected error: {e}")

    async def _do_download_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format: str,
        position: int,
        include_track_num: bool,
        use_album_track_num: bool,
        first_artist_only: bool,
        quality: str,
        artist_separator: str | None = None,
        allow_fallback: bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list | None = None,
        apple_lyrics_word_by_word: bool = True,
        enrich_metadata: bool = False,
        enrich_providers: list | None = None,
        qobuz_token: str | None = None,
        is_album: bool = False,
    ) -> DownloadResult:
        avail = await asyncio.to_thread(
            self.check_availability,
            isrc=metadata.isrc,
            track_name=metadata.title,
            artist_name=metadata.artists,
            options={
                "duration_ms": metadata.duration_ms,
                "spotify_id": metadata.id,
            },
        )

        if not avail.get("available"):
            reason = avail.get("reason", "not found")
            return DownloadResult.fail(self.name, f"Track not available: {reason}")

        track_id = avail.get("track_id", "")
        if not track_id:
            return DownloadResult.fail(
                self.name,
                "checkAvailability returned no track_id",
            )

        ext_hint = _quality_to_ext(quality)
        output_path = self._build_output_path(
            metadata=metadata,
            output_dir=output_dir,
            filename_format=filename_format,
            position=position,
            include_track_num=include_track_num,
            use_album_track_num=use_album_track_num,
            first_artist_only=first_artist_only,
            extension=ext_hint,
            native_id=str(track_id),
        )

        # Everything below writes to output_path, so one download of a
        # given destination happens at a time. Two tracks can resolve to
        # the same filename — a playlist listing a track twice, an album
        # queued alongside its single — and without this they stream into
        # the same .part and rename over each other, leaving a file that
        # looks complete and is not. See core/output_lock.py.
        async with output_path_lock(output_path):
            try:
                for stale in output_path.parent.glob(f"{output_path.stem}.*"):
                    if stale.suffix.lower() in (".flac", ".mp3", ".m4a", ".mp4"):
                        if stale.exists() and stale.stat().st_size == 0:
                            stale.unlink()
                            logger.debug(
                                "[%s] Removed stale zero-byte file: %s",
                                self.name,
                                stale.name,
                            )
            except Exception:
                pass

            if self._file_exists(output_path):
                return DownloadResult.skipped_result(
                    self.name,
                    str(output_path),
                    fmt=_ext_to_fmt(output_path.suffix),
                )

            # Shared between the bridge's real progress events and the disk
            # poller below, so whichever of the two is actually reporting,
            # the other stops guessing over the top of it.
            transfer = _TransferProgress()

            def _progress_adapter(
                fraction: float,
                bytes_received: int | None = None,
                bytes_total: int | None = None,
            ) -> None:
                """Real byte counts from the bridge, when it has them.

                This used to take only the fraction and report it as
                `(percent, 100)` — but the callback's contract is
                `(current_bytes, total_bytes)`, so a 30 MB FLAC drew a bar
                that filled to "97B / 100B" and a transfer speed computed
                from a percentage. `nodeFileDownload` has been emitting
                `bytesReceived`/`bytesTotal` all along and JSRuntime already
                offers them here; nothing was accepting them.
                """
                if self._progress_cb is None:
                    return
                # Whether the bridge counted the bytes is a separate question
                # from whether it knows the total. A chunked response has no
                # Content-Length, so `bytes_total` is 0 while `bytes_received`
                # is exact — treating the two as one condition threw those
                # real counts away and replaced them with the disk poller's,
                # which is the worse number of the two.
                if bytes_received is not None:
                    transfer.note(bytes_received, bytes_total, from_bridge=True)
                    # An estimate is still better than an indeterminate bar,
                    # and estimate_total() declines to make one when the
                    # fraction is too small to divide by.
                    bytes_total = bytes_total or transfer.estimate_total(fraction)
                    if not bytes_received and not bytes_total:
                        # The opening event of a chunked transfer: no bytes
                        # yet and no total. It says nothing the bar can draw,
                        # but noting it above is what stands the poller down.
                        return
                else:
                    # An extension that streams through global.file.download
                    # produces two progress streams for one download: the
                    # bridge's, carrying real byte counts, and its own
                    # onProgress, carrying a bare 0..1. Once the first is
                    # running it is strictly better, so the second is
                    # dropped rather than re-emitting stale numbers over it
                    # and flattening the transfer speed to zero.
                    if transfer.bridge_reporting:
                        return
                    # Otherwise: an extension that writes its own file. Fall
                    # back to what is on disk, which is at least a real
                    # number of bytes.
                    bytes_received = transfer.bytes_on_disk
                    bytes_total = transfer.estimate_total(fraction)
                    if not bytes_received:
                        return
                    transfer.note(bytes_received, bytes_total)
                try:
                    result = self._progress_cb(bytes_received, bytes_total or 0)
                    # If the callback is async and returns a coroutine, we can't await it here
                    # (we're in a sync context via to_thread), so we need to close it to prevent warnings
                    if asyncio.iscoroutine(result):
                        result.close()
                except Exception:
                    pass

            logger.info(
                "[%s] Download starting: '%s' — %s · quality=%s · track_id=%s → %s",
                self.name,
                metadata.title,
                metadata.artists,
                quality,
                track_id,
                output_path.name,
            )
            download_started_at = time.monotonic()

            # ── Progress fallback via disk polling ──────────────────────────
            # Covers the case where the extension bypasses global.file.download
            # (e.g., writing segments manually via file.writeBytes) and thus
            # never generates real "progress" events from the bridge. If instead
            # real events arrive (now emitted by nodeFileDownload in
            # _bridge.js), this fallback is simply redundant and harmless.
            poll_stop = asyncio.Event()
            poll_task = asyncio.create_task(
                self._poll_file_progress_async(output_path, poll_stop, transfer),
            )

            try:
                dl_result = await asyncio.to_thread(
                    self._call,
                    "download",
                    track_id,
                    quality,
                    str(output_path),
                    None,
                    progress_cb=_progress_adapter,
                )
            finally:
                poll_stop.set()
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll_task

            elapsed = time.monotonic() - download_started_at

            if not dl_result or not dl_result.get("success"):
                err = (dl_result or {}).get("error_message", "download failed")
                # The extension's own words, at a level that is visible by
                # default. This is the answer to "it said nothing and no
                # file appeared": either the call never returned a result at
                # all, or it returned one saying why.
                logger.warning(
                    "[%s] Download FAILED for '%s' after %.1fs: %s",
                    self.name,
                    metadata.title,
                    elapsed,
                    err if dl_result else "the extension returned no result",
                )
                return DownloadResult.fail(self.name, err)

            written = transfer.bytes_on_disk
            with contextlib.suppress(OSError):
                if output_path.exists():
                    written = output_path.stat().st_size
            logger.info(
                "[%s] Download finished: '%s' — %s in %.1fs%s",
                self.name,
                metadata.title,
                _human_bytes(written),
                elapsed,
                (
                    f" ({written / elapsed / (1024 * 1024):.1f} MB/s)"
                    if elapsed > 0 and written
                    else ""
                ),
            )

            # ── Reassemble any segments (e.g., Tidal DASH via extension) ──
            actual_path = await self._finalize_segments_async(dl_result, output_path)
            if actual_path is None:
                return DownloadResult.fail(
                    self.name,
                    "Download returned no usable audio (segments unmergeable)",
                )

            if Path(actual_path).suffix.lower() in [".m4a", ".mp4"]:
                codec = await _get_codec_async(actual_path)
                if codec == "flac":
                    logger.info(
                        "[%s] FLAC hidden inside M4A container detected. Starting extraction (remux)...",
                        self.name,
                    )
                    flac_path = str(Path(actual_path).with_suffix(".flac"))

                    d_key = dl_result.get("decryption_key") or dl_result.get(
                        "decryptionKey",
                    )

                    if await _remux_to_flac_async(actual_path, flac_path, d_key):
                        import os

                        with contextlib.suppress(OSError):
                            os.remove(actual_path)
                        actual_path = flac_path
                        logger.info(
                            "[%s] FLAC extraction completed successfully.",
                            self.name,
                        )
                    else:
                        logger.warning(
                            "[%s] FLAC extraction failed, keeping the original file.",
                            self.name,
                        )

            fmt = _ext_to_fmt(Path(actual_path).suffix)

            # ── MusicBrainz, like in native providers (tidal.py, qobuz.py, etc.) ──
            mb_tags: dict[str, str] = {}
            if enrich_metadata and metadata.isrc:
                try:
                    from SpotiFLAC.core.isrc_utils import normalize_isrc
                    from SpotiFLAC.core.musicbrainz import (
                        fetch_mb_metadata_async,
                        mb_result_to_tags,
                    )

                    isrc_clean = normalize_isrc(metadata.isrc)
                    if isrc_clean:
                        # Title/artist/duration are only consulted when the
                        # ISRC turns out not to be linked on MusicBrainz —
                        # which is most of some national catalogues. See
                        # musicbrainz._pick_fallback_recording().
                        mb_data = await fetch_mb_metadata_async(
                            isrc_clean,
                            title=metadata.title,
                            artist=metadata.first_artist or metadata.artists,
                            duration_ms=metadata.duration_ms,
                        )
                        mb_tags = mb_result_to_tags(mb_data)
                except Exception as e:
                    logger.debug(
                        "[%s] MusicBrainz lookup failed (non-fatal): %s",
                        self.name,
                        e,
                    )

            try:
                from SpotiFLAC.core.download_validation import (
                    validate_downloaded_track_async,
                )
                from SpotiFLAC.core.tagger import EmbedOptions, embed_metadata_async

                expected_s = metadata.duration_ms // 1000
                valid, err_msg = await validate_downloaded_track_async(
                    actual_path,
                    expected_s,
                )
                if not valid:
                    logger.warning("[%s] Validation failed: %s", self.name, err_msg)
                    return DownloadResult.fail(
                        self.name, f"Validation failed: {err_msg}"
                    )

                # The duration check above proves the file is a whole track. It
                # cannot prove it is *this* track: a cover, a re-recording or a
                # different artist's song of the same name all run the right
                # length. checkAvailability() returns only {available, track_id},
                # so what the provider resolved is not knowable before the
                # download — but download() reports the title/artist/album it
                # actually fetched, which is enough to catch a wrong match here,
                # before the requested track's tags are written over it.
                mismatch = track_identity_mismatch(
                    expected_title=metadata.title,
                    expected_artist=metadata.artists,
                    expected_album=metadata.album,
                    expected_isrc=metadata.isrc,
                    found_title=str(
                        dl_result.get("title") or dl_result.get("name") or ""
                    ),
                    found_artist=str(
                        dl_result.get("artist") or dl_result.get("artists") or ""
                    ),
                    found_album=str(
                        dl_result.get("album") or dl_result.get("album_name") or ""
                    ),
                    found_isrc=str(dl_result.get("isrc") or ""),
                )
                if mismatch:
                    logger.warning("[%s] Wrong track: %s", self.name, mismatch)
                    with contextlib.suppress(OSError):
                        Path(actual_path).unlink()
                    return DownloadResult.fail(self.name, f"Wrong track: {mismatch}")

                await embed_metadata_async(
                    Path(actual_path),
                    metadata,
                    EmbedOptions(
                        cover_url=metadata.cover_url or "",
                        first_artist_only=first_artist_only,
                        artist_separator=artist_separator,
                        embed_lyrics=embed_lyrics,
                        lyrics_providers=lyrics_providers or [],
                        apple_lyrics_word_by_word=apple_lyrics_word_by_word,
                        enrich=enrich_metadata,
                        enrich_providers=enrich_providers,
                        enrich_qobuz_token=qobuz_token or "",
                        is_album=is_album,
                        extra_tags=mb_tags,
                    ),
                )
            except Exception as e:
                logger.warning("[%s] Tagging failed (non-fatal): %s", self.name, e)

            try:
                for stale in output_path.parent.glob(f"{output_path.stem}.*"):
                    if (
                        stale.suffix.lower() in (".flac", ".mp3", ".m4a", ".mp4")
                        and stale.exists()
                        and stale.stat().st_size == 0
                        and str(stale) != str(actual_path)
                    ):
                        stale.unlink()
                        logger.info(
                            "[%s] Removed leftover zero-byte file: %s",
                            self.name,
                            stale.name,
                        )
            except Exception as exc:
                logger.warning("[%s] Post-download cleanup failed: %s", self.name, exc)

            return DownloadResult.ok(self.name, actual_path, fmt)

    # ─────────────────────── Segment reassembly ────────────────────────

    def _sanctioned_output(
        self,
        file_path: str | None,
        output_path: Path,
    ) -> Path | None:
        """`file_path` as reported by the extension, if it is ours to touch.

        The host chose where this download goes and told the filesystem
        guard about that one directory (see _bridge.js and _fsguard.js).
        What comes back is just a string in the extension's result, and
        everything downstream treats it as the downloaded track: tags are
        written into it, and a track-identity mismatch deletes it. A path
        outside the output directory — a bug in an extension building it
        from unsanitised metadata, or one that means harm — would have that
        happen to a file nobody asked about.

        Symlinks are resolved before the check, so a link planted inside the
        output directory cannot stand in for a file outside it.

        None when there is no usable path, which sends the caller on to the
        segment reassembly below and, failing that, to a failed download —
        the honest outcome, and a loud one.
        """
        if not file_path:
            return None
        try:
            resolved = Path(file_path).resolve()
            allowed = output_path.parent.resolve()
            resolved.relative_to(allowed)
        except ValueError:
            logger.warning(
                "[%s] Extension reported a file outside the output folder; "
                "ignoring it: %s",
                self.name,
                file_path,
            )
            return None
        except OSError as exc:
            logger.warning(
                "[%s] Could not check the reported file path (%s): %s",
                self.name,
                file_path,
                exc,
            )
            return None
        return resolved

    async def _finalize_segments_async(
        self,
        dl_result: dict,
        output_path: Path,
    ) -> str | None:
        """Some extensions (e.g., tidal-web, which downloads via DASH/fMP4) may
        return downloaded segments instead of a single ready audio file.
        Expected contract, in order of priority:

          1. dl_result["file_path"] is a non-empty file inside the output
             directory → no extra work, original behavior. See
             _sanctioned_output() for why the location is checked.
          2. dl_result["segments"] is an ordered list of absolute paths
             (init segment included, if present) → concatenate them as
             raw bytes into a temp file and then remux with ffmpeg
             (same schema as TidalProvider._download_from_manifest_async
             + _mux_audio_async, generalized for any extension).
          3. Defensive fallback: if neither 1 nor 2 apply, but residual files
             exist next to output_path matching the pattern
             "<stem>.partNN" or "<stem>.segNN" left by the extension,
             order them and treat them as (2).

        Returns the path (str) of the final audio file ready for tagging,
        or None if it was not possible to rebuild a valid file.
        """
        file_path = self._sanctioned_output(dl_result.get("file_path"), output_path)
        if file_path and file_path.is_file() and file_path.stat().st_size > 0:
            return str(file_path)

        import re

        def _natural_sort_key(p) -> tuple:
            m = re.search(r"(\d+)(?!.*\d)", p.stem)
            return (int(m.group(1)) if m else 0, p.name)

        segments: list[str] = list(dl_result.get("segments") or [])

        logger.debug(
            "[%s] bridge-provided segments (count=%d): %s",
            self.name,
            len(segments),
            [*segments[:3], "..."] if len(segments) > 3 else segments,
        )

        if not segments:
            parent = output_path.parent
            stem = output_path.stem

            init_candidates = sorted(parent.glob(f"{stem}.init*"))
            media_candidates = sorted(
                parent.glob(f"{stem}.part*"),
                key=_natural_sort_key,
            ) or sorted(
                parent.glob(f"{stem}.seg*"),
                key=_natural_sort_key,
            )

            ordered = [
                p
                for p in (init_candidates + media_candidates)
                if p.exists() and p.stat().st_size > 0
            ]
            segments = [str(p) for p in ordered]

            logger.debug(
                "[%s] fallback glob found %d segment file(s) (init=%d, media=%d), first=%s last=%s",
                self.name,
                len(segments),
                len(init_candidates),
                len(media_candidates),
                segments[0] if segments else None,
                segments[-1] if segments else None,
            )

        if not segments:
            return None

        logger.info(
            "[%s] Reassembling %d segment(s) into a single stream…",
            self.name,
            len(segments),
        )

        raw_concat = output_path.with_suffix(".raw.tmp")
        try:
            with open(raw_concat, "wb") as out_f:
                for seg_path in segments:
                    with open(seg_path, "rb") as seg_f:
                        out_f.write(seg_f.read())
        except Exception as e:
            logger.exception("[%s] Segment concatenation failed: %s", self.name, e)
            if raw_concat.exists():
                raw_concat.unlink(missing_ok=True)
            return None

        codec = "flac"
        try:
            rc, stdout, stderr = await self._run_ffprobe(
                "ffprobe",
                "-v",
                "quiet",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(raw_concat),
            )
            if rc == 0 and stdout.strip():
                codec = stdout.strip().lower()
            else:
                logger.warning(
                    "[%s] ffprobe failed on reassembled stream, guessing codec: %s",
                    self.name,
                    stderr.strip()[:150],
                )
        except Exception:
            logger.warning(
                "[%s] ffprobe failed to detect codec after reassembly",
                self.name,
            )

        is_lossy = codec not in ("flac", "alac")
        final_dest = (
            output_path.with_suffix(".m4a")
            if is_lossy
            else output_path.with_suffix(".flac")
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
                with contextlib.suppress(Exception):
                    Path(seg_path).unlink(missing_ok=True)

        if rc != 0:
            logger.error("[%s] ffmpeg mux failed: %s", self.name, stderr[:15000])
            if final_dest.exists():
                final_dest.unlink(missing_ok=True)
            return None

        logger.info("[%s] Segments merged into: %s", self.name, final_dest.name)
        return str(final_dest)

    # ─────────────────────── Progress polling fallback ─────────────────

    async def _poll_file_progress_async(
        self,
        output_path: Path,
        stop_event: asyncio.Event,
        transfer: _TransferProgress,
    ) -> None:
        """Monitors the growth of output_path (or its common temporary variants:
        .part, .tmp, .download) and feeds self._progress_cb with the real
        number of bytes on disk when no "progress" events arrive from the JS
        bridge (e.g., extensions that write their own files without passing
        through global.file.download).

        It reports bytes rather than a made-up percentage. The old version
        emitted `1 - 1/(1 + polls * 0.15)` as a percent-of-100 "byte" count,
        which moved the bar on a timer whether or not anything was being
        transferred, and told the bar the file was 100 bytes long. Bytes on
        disk are a real measurement; the total stays unknown unless the
        bridge supplies one, and an unknown total draws an indeterminate bar
        instead of a wrong one.
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

        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.3)

                size = 0
                for cand in candidates:
                    try:
                        if cand.exists():
                            size = max(size, cand.stat().st_size)
                    except OSError:
                        continue

                if size <= 0:
                    continue

                transfer.bytes_on_disk = size

                # The bridge is reporting real byte counts for this
                # download, so it owns the bar; polling would only fight it.
                if transfer.bridge_reporting:
                    continue

                if size != last_size:
                    last_size = size
                    try:
                        result = self._progress_cb(size, transfer.total_bytes)
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
    if "flac" in q:
        return ".flac"
    if "mp3" in q:
        return ".mp3"
    if "aac" in q or "m4a" in q:
        return ".m4a"
    if "opus" in q:
        return ".opus"
    return ".flac"


def _ext_to_fmt(suffix: str) -> str:
    return {".flac": "flac", ".mp3": "mp3", ".m4a": "m4a"}.get(suffix.lower(), "flac")


async def _get_codec_async(filepath: str) -> str:
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            filepath,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip().lower()
    except Exception:
        return "m4a"


async def _remux_to_flac_async(
    input_path: str,
    output_path: str,
    decryption_key: str | None = None,
) -> bool:
    import logging

    logger = logging.getLogger(__name__)
    try:
        cmd = ["ffmpeg", "-y"]
        if decryption_key:
            cmd.extend(["-decryption_key", str(decryption_key).strip()])

        cmd.extend(["-i", input_path, "-map", "0:a:0", "-c:a", "flac", output_path])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        from pathlib import Path

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="ignore")[-150:].replace("\n", " ")
            logger.warning("Errore FFMPEG: %s", err_msg)
            return False
        return Path(output_path).exists()
    except Exception as exc:
        logger.warning("FLAC remux error: %s", exc)
        return False


def make_extension_provider(
    ext_id: str,
    settings: dict | None = None,
    ext_dir: str | None = None,
    node_executable: str = "node",
    timeout_s: int = 180,
) -> JSExtensionProvider:
    return JSExtensionProvider(
        ext_id=ext_id,
        settings=settings,
        ext_dir=ext_dir,
        node_executable=node_executable,
        timeout_s=timeout_s,
    )

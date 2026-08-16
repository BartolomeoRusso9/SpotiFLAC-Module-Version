"""Downloader — main orchestrator (100% Async Native).
Changes compared to the original:
  - DownloadOptions: +track_max_retries, +post_download_action, +post_download_command
  - download_one_async(): per-track retry with exponential backoff and pure async flow
  - DownloadWorker.run_async(): async semaphores for concurrent task orchestration
  - SpotiflacDownloader.run_async(): fully async batch processing and metadata fetching
  - 100% Asynchronous I/O wrappers for filesystem operations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .core.console import (
    print_playlist_resolved,
    print_playlist_summary,
    print_run_header,
    print_summary,
    print_sync_plan,
    print_track_done,
    print_track_header,
    print_track_skipped,
)
from .core.errors import ErrorKind, SpotiflacError
from .core.http import AsyncHttpClient
from .core.isrc_helper import IsrcHelper
from .core.models import DownloadResult, TrackMetadata, build_filename
from .core.playlist_sync import (
    PlaylistSource,
    SyncPlan,
    build_plan,
    entry_for,
    index_audio_files,
    mark_existing,
    render_m3u,
    write_if_changed_async,
)
from .core.progress import (
    DownloadManager,
    ProgressCallback,
    ProgressManager,
    install_console_interception,
    safe_tqdm_write,
    uninstall_console_interception,
)
from .core.quality import normalize_quality
from .core.spotify_metadata import SpotifyMetadataClient
from .core.transcode import (
    DEFAULT_MP3_BITRATE,
    ensure_ffmpeg_available,
    normalize_bitrate,
    normalize_transcode_format,
    transcode_file_async,
    transcoded_file_exists,
)

if TYPE_CHECKING:
    from .core.base import BaseProvider

logger = logging.getLogger(__name__)


async def _call_metadata_get_url(client, url: str, **kwargs):
    """Calls client.get_url_async(url, **kwargs) if it exists, otherwise
    client.get_url(url, **kwargs) — awaiting it directly if it's already
    a coroutine function, or offloading to a thread only if it's truly sync.
    """
    fn = getattr(client, "get_url_async", None)
    if fn is None:
        fn = client.get_url

    if asyncio.iscoroutinefunction(fn):
        return await fn(url, **kwargs)
    return await asyncio.to_thread(fn, url, **kwargs)


def _adapt_js_metadata_response(response):
    """Adapt JSExtensionProvider dict response to expected tuple format.

    JSExtensionProvider may return a dict with keys like:
    {'collection_name': str, 'tracks': list, 'collection_cover': str (optional)}

    This adapter converts it to the tuple format expected by the caller:
    (collection_name, tracks, *optional_cover)
    """

    # If response is already a tuple/list, return as-is (native Python provider format)
    if isinstance(response, (tuple, list)):
        return response

    # If it's a dict (JS provider format), convert to tuple
    if isinstance(response, dict):
        collection_name = response.get("collection_name", "Unknown")
        tracks = response.get("tracks", [])
        collection_cover = response.get("collection_cover")
        if collection_cover:
            return (collection_name, tracks, collection_cover)
        return (collection_name, tracks)

    # Fallback: return as-is
    return response


@dataclass
class DownloadOptions:
    output_dir: str
    services: list[str] = field(default_factory=lambda: ["ext:tidal-web"])
    filename_format: str | Callable[..., str] = "{title} - {artist}"
    use_track_numbers: bool = False
    use_album_track_numbers: bool = False
    use_artist_subfolders: bool = False
    use_album_subfolders: bool = False
    # When True, each playlist download is placed in a subfolder named after
    # the playlist. Set to False to keep playlist downloads flat in the
    # `output_dir` (useful for music libraries).
    create_playlist_subfolders: bool = True
    first_artist_only: bool = False
    include_featuring: bool = False
    quality: str = "LOSSLESS"
    allow_fallback: bool = True
    inter_track_delay_s: float = 1.0
    is_album: bool = False
    output_path: str | None = None

    embed_lyrics: bool = True
    lyrics_providers: list[str] = field(
        default_factory=lambda: ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
    )

    enrich_metadata: bool = True
    enrich_providers: list[str] = field(
        default_factory=lambda: ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
    )
    qobuz_token: str | None = None
    qobuz_local_api_url: str | None = None

    # Post-download conversion: None = keep the provider format,
    # "mp3" = convert every track to MP3 at `transcode_bitrate`.
    # The converted file uses the same name with a different extension so
    # skipping already-downloaded tracks still works (the converted file is
    # looked for directly before contacting providers).
    transcode_to: str | None = None
    transcode_bitrate: str = DEFAULT_MP3_BITRATE
    transcode_keep_original: bool = False

    track_max_retries: int = 0
    post_download_action: str = "none"
    post_download_command: str = ""
    tidal_custom_api: str | None = None
    timeout_s: int | None = None
    auto_pair_extensions: bool = True
    ext_dir: str | None = None
    # Phase 2: maximum concurrent downloads managed by the semaphore
    # asyncio.Semaphore in DownloadWorker._run_downloads_async(). Previously
    # this was a hardcoded constant (MAX_CONCURRENT_DOWNLOADS = 2) — now it is
    # configurable by the caller (CLI/API), while preserving the same default.
    max_concurrent_downloads: int = 2

    def __post_init__(self) -> None:
        # Normalize immediately so the rest of the code can do `if opts.transcode_to`
        # and an unsupported format fails where it is configured, not midway
        # through a download batch.
        self.transcode_to = normalize_transcode_format(self.transcode_to)
        self.transcode_bitrate = normalize_bitrate(self.transcode_bitrate)


def _build_providers_for_name(name: str, opts: DownloadOptions) -> list[BaseProvider]:
    """Build the provider list for a service name.

    Returns a list with the native Python extension (if installed) first,
    followed by the JavaScript extension as a fallback.
    Respects explicit requests like 'ext:qobuz-web' or 'ext:qobuz-py'.
    """
    from .extensions.catalog import extension_id
    from .extensions.manager import ExtensionManager
    from .extensions.provider import JSExtensionProvider

    providers: list[BaseProvider] = []
    try:
        manager = ExtensionManager(ext_dir=opts.ext_dir, auto_install_downloads=True)

        original_ext_id = extension_id(name, manager)
        base_name = (
            original_ext_id.lower()
            .replace("-web", "")
            .replace("ext:", "")
            .replace("-py", "")
        )

        # Analizza l'intento esplicito dell'utente
        wants_explicit_js = "-web" in name.lower()
        wants_explicit_py = "-py" in name.lower()

        # 1. TENTATIVO PYTHON (Priorità 1)
        # Se l'utente NON ha digitato esplicitamente "-web", prova ad usare Python
        if not wants_explicit_js:
            py_candidate_name = manager.find_python_extension(base_name)

            if py_candidate_name:
                try:
                    from .extensions.python_provider import PythonExtensionProvider

                    py_prov = PythonExtensionProvider(
                        py_candidate_name, ext_dir=opts.ext_dir
                    )
                    providers.append(py_prov)
                    logger.debug(
                        "Added Python provider candidate: %s", py_candidate_name
                    )
                except Exception as e_py:
                    logger.warning(
                        "Python extension '%s' failed to initialize: %s",
                        py_candidate_name,
                        e_py,
                    )

        # 2. TENTATIVO JAVASCRIPT (Priorità 2: Fallback)
        # Se l'utente NON ha digitato esplicitamente "-py", aggiunge JS (o come fallback, o come primario)
        # Rispetta opts.auto_pair_extensions per il fallback automatico
        if not wants_explicit_py and (wants_explicit_js or opts.auto_pair_extensions):
            try:
                js_prov = JSExtensionProvider(
                    original_ext_id,
                    ext_dir=opts.ext_dir,
                    timeout_s=opts.timeout_s or 120,
                )
                providers.append(js_prov)
                logger.debug("Added JS provider fallback: %s", original_ext_id)
            except Exception as e_js:
                logger.debug(
                    "JS extension fallback not available for '%s': %s",
                    original_ext_id,
                    e_js,
                )

    except Exception as e:
        logger.warning("Failed to resolve providers for %s: %s", name, e)

    return providers


def _no_providers_error_message(services: list[str]) -> str:
    """Builds an actionable error when no provider (Python or JS extension)
    could be resolved for any requested service.

    SpotiFLAC now downloads exclusively through installed extensions — there
    are no built-in native providers anymore. The most common cause of an
    empty provider list is that no extension registry is configured at all,
    so nothing was ever installed. Distinguish that case (fixable by setting
    SPOTIFLAC_REGISTRIES) from the case where registries ARE configured but
    still failed to produce a usable extension for these specific services
    (network issue, registry doesn't list them, etc.).
    """
    services_str = ", ".join(services)

    urls: list[str] = []
    try:
        from .extensions import registry_config

        urls = registry_config.effective_urls()
    except Exception as e:
        logger.debug("[downloader] Unable to inspect registry config: %s", e)

    if not urls:
        return (
            f"No extensions found for: [{services_str}]. SpotiFLAC downloads "
            "exclusively through installed extensions, and no extension "
            "registry is currently configured, so none were ever installed.\n"
            "Fix: export a registry URL with SPOTIFLAC_REGISTRIES, or add it "
            "to a .env file (in the project folder or ~/.spotiflac_env), e.g.:\n"
            '  export SPOTIFLAC_REGISTRIES="https://your-registry-url/registry.json"\n'
            "or in .env:\n"
            "  SPOTIFLAC_REGISTRIES=https://your-registry-url/registry.json\n"
            "(comma-separate multiple registry URLs)."
        )

    registry_word = "registry is" if len(urls) == 1 else "registries are"
    return (
        f"No valid providers found in: [{services_str}]. {len(urls)} extension "
        f"{registry_word} configured ({', '.join(urls)}) but none of them "
        "produced a working extension for these services. Check your network "
        "connection, or that these services are actually listed in the "
        "registry."
    )


async def _move_file_async(src: str, dst: str) -> None:
    """Async thread-safe helper to rename/move files."""

    def _do_move() -> None:
        os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dst):
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)

    await asyncio.to_thread(_do_move)


async def _get_file_size_mb_async(path: str) -> float:
    """Async thread-safe helper to calculate file size in MB."""

    def _do_get():
        if path and os.path.exists(path):
            return os.path.getsize(path) / (1024 * 1024)
        return 0.0

    return await asyncio.to_thread(_do_get)


def transcode_target_path(
    metadata: TrackMetadata,
    output_dir: str,
    opts: DownloadOptions,
    position: int = 1,
) -> Path | None:
    """Final path a track will have once transcoded, or None if transcoding is off.

    Mirrors the naming used by `BaseProvider._build_output_path()` — same
    template, same options, only a different extension — so the file can be
    looked up before any provider is contacted.
    """
    if not opts.transcode_to:
        return None

    extension = f".{opts.transcode_to}"
    if opts.output_path:
        base, _ = os.path.splitext(opts.output_path)
        return Path(base + extension)

    filename = build_filename(
        metadata,
        fmt=opts.filename_format,
        position=position,
        include_track_number=opts.use_track_numbers,
        use_album_track_number=opts.use_album_track_numbers,
        first_artist_only=opts.first_artist_only,
        extension=extension,
    )
    return Path(output_dir) / filename


async def _transcode_result_async(
    result: DownloadResult,
    opts: DownloadOptions,
) -> DownloadResult:
    """Converts a finished download to `opts.transcode_to`.

    A result whose file is already in the target format is returned untouched,
    which also covers providers that natively deliver MP3.
    """
    source = Path(result.file_path or "")
    if not result.file_path or source.suffix.lower() == f".{opts.transcode_to}":
        return result

    try:
        dest = await transcode_file_async(
            source,
            fmt=opts.transcode_to,
            bitrate=opts.transcode_bitrate,
            keep_original=opts.transcode_keep_original,
        )
    except Exception as exc:
        logger.warning("[transcode] %s: %s", source.name, exc)
        return DownloadResult.fail(
            result.provider,
            f"Downloaded, but transcode to {opts.transcode_to.upper()} failed: {exc}",
        )

    # Even a "skipped" result (file already existing in another format) is
    # rewritten: it should be reported as a successful download, not as a skip.
    return DownloadResult.ok(result.provider, str(dest), opts.transcode_to)


async def download_one_async(
    metadata: TrackMetadata,
    output_dir: str,
    providers: list[BaseProvider],
    opts: DownloadOptions,
    position: int = 1,
    is_album: bool = False,
) -> DownloadResult:
    """Attempts to download a single track across all providers in order,
    with per-track retry if track_max_retries > 0.
    """
    stop_event = asyncio.Event()
    DownloadManager()
    errors: dict[str, str] = {}
    started_at = time.monotonic()

    transcode_target = transcode_target_path(metadata, output_dir, opts, position)
    if transcode_target and transcoded_file_exists(transcode_target):
        print_track_skipped(
            metadata.title,
            f"already downloaded as {opts.transcode_to.upper()}",
        )
        logger.info(
            "[transcode] ⏭ already downloaded as %s: %s — %s",
            opts.transcode_to.upper(),
            metadata.artists,
            metadata.title,
        )
        return DownloadResult.skipped_result(
            providers[0].name if providers else "none",
            str(transcode_target),
            fmt=opts.transcode_to,
        )

    for attempt in range(opts.track_max_retries + 1):
        if stop_event.is_set() or (
            opts.timeout_s and time.monotonic() - started_at >= opts.timeout_s
        ):
            return DownloadResult.fail(
                "none",
                f"Download timed out after {opts.timeout_s}s",
            )

        if attempt > 0:
            wait = min(2**attempt, 30)
            safe_tqdm_write(
                f"\n  ↺  Retry {attempt}/{opts.track_max_retries} in {wait}s…",
            )
            await asyncio.sleep(wait)
            errors.clear()

        for idx, provider in enumerate(providers):
            if idx > 0:
                is_ext = provider.name.startswith("ext:")
                target_type = "extension" if is_ext else "provider"
                safe_tqdm_write(
                    f"  ⚠️  Fallback: switching to backup {target_type} ({provider.name})...",
                )

            logger.info(
                "[%s] Trying: %s — %s",
                provider.name,
                metadata.artists,
                metadata.title,
            )
            cb = ProgressCallback(item_id=metadata.id, track_name=metadata.title)
            provider.set_progress_callback(cb)

            # Cooperative shutdown propagation
            if hasattr(provider, "set_stop_event_async"):
                with contextlib.suppress(Exception):
                    provider.set_stop_event_async(stop_event)

            try:
                # Wrap inside asyncio.wait_for to enforce track timeout strictly at the IO level
                time_elapsed = time.monotonic() - started_at
                timeout_left = (
                    max(1, opts.timeout_s - time_elapsed) if opts.timeout_s else None
                )

                download_task = provider.download_track_async(
                    metadata,
                    output_dir,
                    filename_format=opts.filename_format,
                    position=position,
                    include_track_num=opts.use_track_numbers,
                    use_album_track_num=opts.use_album_track_numbers,
                    first_artist_only=opts.first_artist_only,
                    allow_fallback=opts.allow_fallback,
                    embed_lyrics=opts.embed_lyrics,
                    lyrics_providers=opts.lyrics_providers,
                    enrich_metadata=opts.enrich_metadata,
                    enrich_providers=opts.enrich_providers,
                    is_album=is_album,
                    quality=normalize_quality(opts.quality),
                    qobuz_token=opts.qobuz_token,
                )

                if timeout_left:
                    result = await asyncio.wait_for(download_task, timeout=timeout_left)
                else:
                    result = await download_task

            except asyncio.TimeoutError:
                stop_event.set()
                logger.warning(
                    "[downloader] timeout exceeded for track '%s'",
                    metadata.title,
                )
                safe_tqdm_write(
                    f"\n  ⏱  Timeout reached for '{metadata.title}' — skipping track.",
                )
                return DownloadResult.fail(
                    "none",
                    f"Download timed out after {opts.timeout_s}s",
                )

            if result.success:
                if opts.transcode_to:
                    # A file already existing in another format is also converted:
                    # on the next pass the skip logic will already find it in MP3.
                    result = await _transcode_result_async(result, opts)
                    if not result.success:
                        return result

                if result.skipped:
                    print_track_skipped(metadata.title, "already in the output folder")
                    logger.info(
                        "[%s] ⏭ %s — %s",
                        provider.name,
                        metadata.artists,
                        metadata.title,
                    )
                    return result
                if opts.output_path and result.file_path:
                    _, ext = os.path.splitext(result.file_path)
                    base_target, _ = os.path.splitext(opts.output_path)
                    target = base_target + ext
                    # Spostamento delegato all'I/O asincrono
                    await _move_file_async(result.file_path, target)
                    result = DownloadResult.ok(
                        result.provider,
                        target,
                        result.format or "flac",
                    )

                print_track_done(
                    result.provider or provider.name,
                    metadata.title,
                    result.format or "flac",
                    await _get_file_size_mb_async(result.file_path) * 1024 * 1024,
                    time.monotonic() - started_at,
                )
                logger.info(
                    "[%s] ✓ %s — %s",
                    provider.name,
                    metadata.artists,
                    metadata.title,
                )
                return result

            errors[provider.name] = result.error or "unknown error"
            safe_tqdm_write(f"  ✗  {provider.name}  ·  {result.error}", file=sys.stderr)
            logger.debug("[%s] ✗ %s", provider.name, result.error)

    attempts_str = f"{opts.track_max_retries + 1} attempt(s)"
    summary = "; ".join(f"{k}: {v}" for k, v in errors.items())
    return DownloadResult.fail(
        "none",
        f"All providers failed after {attempts_str} — {summary}",
    )


# ---------------------------------------------------------------------------
# Post-download actions helpers (Async)
# ---------------------------------------------------------------------------


async def _send_system_notify_async(title: str, body: str) -> None:
    """Sends a system notification asynchronously."""
    try:
        if sys.platform == "darwin":
            script = f'display notification "{body}" with title "{title}"'
            await asyncio.create_subprocess_exec("osascript", "-e", script)
        elif sys.platform == "win32":
            pass
        else:
            await asyncio.create_subprocess_exec("notify-send", title, body)
    except Exception:
        pass


async def _open_folder_async(path: str) -> None:
    """Opens the folder in the system file manager asynchronously."""
    try:
        if sys.platform == "darwin":
            await asyncio.create_subprocess_exec("open", path)
        elif sys.platform == "win32":
            await asyncio.create_subprocess_exec("explorer", os.path.normpath(path))
        else:
            await asyncio.create_subprocess_exec("xdg-open", path)
    except Exception as exc:
        logger.warning("[post-action] open_folder failed: %s", exc)


# ---------------------------------------------------------------------------
# DownloadWorker
# ---------------------------------------------------------------------------


class DownloadWorker:
    def __init__(
        self,
        tracks: list[TrackMetadata],
        opts: DownloadOptions,
        collection_name: str = "",
        is_album: bool = False,
        is_playlist: bool = False,
        positions: list[int] | None = None,
    ) -> None:
        self._tracks = tracks
        self._opts = opts
        self._collection_name = collection_name
        self._is_album = is_album
        self._is_playlist = is_playlist
        # Track number used for the filename. By default it's the position in
        # the list; a caller that downloads only a subset (e.g. multi-playlist
        # sync that skips already-present tracks) passes original positions so
        # file names do not change between runs.
        self._positions = positions or list(range(1, len(tracks) + 1))
        self._failed: list[tuple[str, str, str, str]] = []
        self._skipped: list[tuple[str, str]] = []
        self._completed: dict[str, str] = {}
        self._providers: list[BaseProvider] = self._build_providers()

    @property
    def completed_paths(self) -> dict[str, str]:
        """Track id → final file path, for every track available on disk.

        Includes the tracks that were skipped because they were already
        downloaded: from the caller's point of view the file is there either
        way.
        """
        return dict(self._completed)

    def _build_providers(self) -> list[BaseProvider]:
        result = []
        for name in self._opts.services:
            result.extend(_build_providers_for_name(name, self._opts))
        if not result:
            raise ValueError(_no_providers_error_message(self._opts.services))
        return result

    def _close_providers(self) -> None:
        for provider in self._providers:
            close = getattr(provider, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

    async def run_async(self) -> list[tuple[str, str, str]]:
        try:
            if self._opts.transcode_to:
                # It's better to fail fast than to download a whole album and
                # discover only at the end that conversion is not possible.
                await asyncio.to_thread(
                    ensure_ffmpeg_available,
                    self._opts.transcode_to,
                )

            manager = DownloadManager()
            await manager.reset()
            total = len(self._tracks)
            start = time.perf_counter()

            # Native async folder I/O delegation
            base_out = await self._resolve_output_dir_async()

            print_run_header(
                total,
                self._opts.services,
                normalize_quality(self._opts.quality),
                base_out,
                max(1, self._opts.max_concurrent_downloads),
            )

            install_console_interception()
            ProgressManager.initialize_master_bar(total, description="Progress")
            try:
                return await self._run_downloads_async(manager, total, base_out, start)
            finally:
                await ProgressManager.clear_all()
                uninstall_console_interception()
        finally:
            self._close_providers()

    async def _run_downloads_async(
        self,
        manager: DownloadManager,
        total: int,
        base_out: str,
        start: float,
    ) -> list[tuple[str, str, str]]:
        """Fase 2 — concorrenza nativa asyncio.

        Before: a list of asyncio.Task consumed with asyncio.as_completed().
        Functionally correct, but without structured error propagation
        (a task raising an unexpected exception did not cancel the others,
        and cancellation had to be handled manually).

        Now: asyncio.TaskGroup (structured concurrency, PEP 654/3.11+).
        Rate limiting remains an asyncio.Semaphore(max_concurrent_downloads)
        acquired by each worker before performing heavy I/O (network requests /
        disk writes). Results are processed as they arrive through an internal
        asyncio.Queue, so progress bar updates remain incremental like the
        as_completed version, but inside a TaskGroup that ensures: if a worker
        raises an unexpected exception, all other tasks in the group are
        cleanly cancelled instead of continuing.
        silenziosamente.
        """
        max_concurrent = max(1, getattr(self._opts, "max_concurrent_downloads", 2))
        semaphore = asyncio.Semaphore(max_concurrent)
        results_queue: asyncio.Queue[tuple[TrackMetadata, object] | None] = (
            asyncio.Queue()
        )

        async def download_worker(i: int, track: TrackMetadata) -> None:
            position = self._positions[i]
            async with semaphore:
                print_track_header(
                    i + 1,
                    total,
                    track.title,
                    track.artists,
                    track.album,
                )
                await manager.start_download(track.id)

                out_dir = await self._track_output_dir_async(base_out, track)
                try:
                    result = await download_one_async(
                        track,
                        out_dir,
                        self._providers,
                        self._opts,
                        position,
                        self._is_album,
                    )
                except Exception as exc:
                    logger.exception(
                        "[worker] Unexpected exception downloading '%s'",
                        track.title,
                    )
                    result = DownloadResult.fail("none", f"Unexpected error: {exc}")

            await results_queue.put((track, result))

        async def consume_results() -> None:
            for _ in range(total):
                track, result = await results_queue.get()

                if result.success and result.file_path:
                    self._completed[track.id] = result.file_path

                if result.success and result.skipped:
                    await manager.skip_download(track.id)
                    self._skipped.append((track.id, track.title))
                elif result.success:
                    size_mb = await _get_file_size_mb_async(result.file_path)
                    await manager.complete_download(
                        track.id,
                        result.file_path or "",
                        size_mb,
                    )
                else:
                    err = result.error or "unknown"
                    self._failed.append((track.id, track.title, track.artists, err))
                    safe_tqdm_write(
                        f"\n  ✗  Failed: {track.title} — {track.artists}: {err}",
                        file=sys.stderr,
                    )
                    logger.debug(
                        "[worker] Failed: %s — %s: %s",
                        track.title,
                        track.artists,
                        err,
                    )
                    await manager.fail_download(track.id, err)
                    ProgressCallback.clear_item(track.id)

                ProgressManager.increment_master()

        consumer_task = asyncio.create_task(consume_results())
        worker_tasks = [
            asyncio.create_task(download_worker(i, track))
            for i, track in enumerate(self._tracks)
        ]

        try:
            await asyncio.gather(consumer_task, *worker_tasks)
        except Exception:
            consumer_task.cancel()
            for t in worker_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(consumer_task, *worker_tasks, return_exceptions=True)
            raise

        elapsed = time.perf_counter() - start
        self._print_summary(elapsed)
        await self._execute_post_action_async(base_out)
        return self._failed

    async def _resolve_output_dir_async(self) -> str:
        """Asynchronously resolves the output directory ensuring it exists."""

        def _do_resolve():
            if self._opts.output_path:
                out = os.path.normpath(
                    os.path.dirname(os.path.abspath(self._opts.output_path)),
                )
                os.makedirs(out, exist_ok=True)
                return out

            out = os.path.normpath(self._opts.output_dir)
            if (
                self._is_playlist
                and self._collection_name
                and getattr(self._opts, "create_playlist_subfolders", True)
            ) or (
                self._is_album
                and self._collection_name
                and not self._opts.use_album_subfolders
            ):
                safe_name = re.sub(r'[<>:"/\\|?*]', "_", self._collection_name.strip())
                out = os.path.join(out, safe_name)

            os.makedirs(out, exist_ok=True)
            return out

        return await asyncio.to_thread(_do_resolve)

    async def _track_output_dir_async(self, base: str, track: TrackMetadata) -> str:
        """Asynchronously creates any subfolders based on artist or album."""

        def _do_track_dir():
            out = base
            if self._opts.use_artist_subfolders:
                folder = re.sub(r'[<>:"/\\|?*]', "_", track.first_artist)
                out = os.path.join(out, folder)
            if self._opts.use_album_subfolders:
                folder = re.sub(r'[<>:"/\\|?*]', "_", track.album)
                out = os.path.join(out, folder)
            os.makedirs(out, exist_ok=True)
            return out

        return await asyncio.to_thread(_do_track_dir)

    def _print_summary(self, elapsed: float) -> None:
        succeeded = len(self._tracks) - len(self._failed) - len(self._skipped)
        skipped_count = len(self._skipped)
        display = [(t, a, e) for _, t, a, e in self._failed]
        print_summary(len(self._tracks), succeeded, skipped_count, display, elapsed)

    async def _execute_post_action_async(self, output_dir: str) -> None:
        action = self._opts.post_download_action
        if not action or action == "none":
            return

        succeeded = len(self._tracks) - len(self._failed) - len(self._skipped)
        skipped_count = len(self._skipped)
        failed_count = len(self._failed)

        if action == "open_folder":
            await _open_folder_async(output_dir)

        elif action == "notify":
            body = f"{succeeded} tracks downloaded"
            if skipped_count:
                body += f", {skipped_count} skipped"
            if failed_count:
                body += f", {failed_count} failed"
            await _send_system_notify_async("SpotiFLAC — Download completed", body)

        elif action == "command":
            cmd_template = self._opts.post_download_command
            if not cmd_template:
                logger.warning(
                    "[post-action] action=command but post_download_command is empty",
                )
                return
            cmd = (
                cmd_template.replace("{folder}", output_dir)
                .replace("{succeeded}", str(succeeded))
                .replace("{skipped}", str(skipped_count))
                .replace("{failed}", str(failed_count))
            )
            try:
                await asyncio.create_subprocess_shell(cmd)
            except Exception as exc:
                logger.warning("[post-action] command failed: %s", exc)

        else:
            logger.warning("[post-action] unknown action: %s", action)


# ---------------------------------------------------------------------------
# SpotiflacDownloader
# ---------------------------------------------------------------------------


class SpotiflacDownloader:
    def __init__(self, opts: DownloadOptions) -> None:
        self._opts = opts
        # Metadata is only needed for Spotify URLs.  Keeping it lazy prevents
        # construction from performing network work for extension URLs, tests,
        # and playlist operations that inject their own metadata source.
        self._client: SpotifyMetadataClient | None = None

    def _metadata_client(self) -> SpotifyMetadataClient:
        if self._client is None:
            self._client = SpotifyMetadataClient()
        return self._client

    async def run_async(
        self,
        input_url: str | list[str],
        loop_minutes: int | None = None,
    ) -> None:
        """Starts downloading one or more URLs using the async worker pipeline."""
        urls = [input_url] if isinstance(input_url, str) else list(input_url)

        for _idx, url in enumerate(urls):
            if len(urls) > 1:
                pass

            failed_tracks = None
            while True:
                failed_tracks = await self._run_once_async(
                    url,
                    target_tracks=failed_tracks,
                )
                if not loop_minutes or loop_minutes <= 0 or not failed_tracks:
                    break
                await asyncio.sleep(loop_minutes * 60)

    # ------------------------------------------------------------------
    # Multi-playlist sync
    # ------------------------------------------------------------------

    async def run_playlists_async(
        self,
        urls: list[str],
        m3u_format: str = "m3u8",
    ) -> None:
        """Downloads several playlists into one folder, one M3U file each.

        A track shared by two playlists is downloaded once, tracks already in
        the output directory are never fetched again, and each playlist gets an
        M3U file rewritten only when its content changed — so running this
        again after a playlist gained a track only downloads that track.
        """
        opts = self._playlist_opts()
        sources = await self._resolve_playlists_async(urls)
        if not sources:
            logger.warning("[playlists] No playlist could be resolved")
            return

        plan = build_plan(sources, m3u_format=m3u_format)
        output_dir = Path(opts.output_dir)
        await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
        index = await asyncio.to_thread(index_audio_files, output_dir)
        plan = mark_existing(plan, index, opts)
        print_sync_plan(len(plan.tracks), len(plan.present), len(plan.pending))

        located: dict[str, Path] = {
            planned.key: planned.existing_path for planned in plan.present
        }
        located.update(await self._download_pending_async(plan, opts))
        await self._write_playlist_files_async(plan, located, output_dir, m3u_format)

    def _playlist_opts(self) -> DownloadOptions:
        """Options adjusted for a flat, multi-playlist run."""
        opts = self._opts
        if opts.output_path:
            logger.warning(
                "[playlists] --output-path ignored: every track is saved in the "
                "output directory with standard renaming.",
            )
            opts = replace(opts, output_path=None)

        uses_position = (
            isinstance(opts.filename_format, str)
            and "{position}" in opts.filename_format
        )
        if opts.use_track_numbers or uses_position:
            logger.warning(
                "[playlists] track numbers depend on the merged playlist order: "
                "filenames will change whenever a playlist does, and already "
                "downloaded tracks are then fetched again under the new name.",
            )
        return opts

    async def _resolve_playlists_async(self, urls: list[str]) -> list[PlaylistSource]:
        """Fetches every playlist, keeping the run alive when one fails."""
        sources: list[PlaylistSource] = []
        for url in urls:
            try:
                collection_name, tracks, info = await self._resolve_metadata_async(url)
            except SpotiflacError as exc:
                # Una playlist irraggiungibile non deve far saltare le altre.
                logger.error("[playlists] %s: %s", url, exc)
                continue

            if not tracks:
                logger.warning("[playlists] No track found: %s", url)
                continue

            name = collection_name or "Playlist"
            # Before ISRC resolution: that can take a long time, and knowing
            # which playlist is being processed is useful right there.
            print_playlist_resolved(name, len(tracks), url)

            # SoundCloud e Pandora non espongono ISRC: la risoluzione bulk
            # sarebbe solo tempo perso (come in _run_once_async).
            lowered = url.lower()
            if not any(
                host in lowered
                for host in ("soundcloud.com", "pandora.com", "pandora.app.link")
            ):
                tracks = await self._resolve_isrc_bulk_async(tracks)

            await self._record_history_async(url, collection_name, tracks, info)
            sources.append(
                PlaylistSource(url=url, name=name, tracks=tuple(tracks)),
            )
        return sources

    async def _download_pending_async(
        self,
        plan: SyncPlan,
        opts: DownloadOptions,
    ) -> dict[str, Path]:
        """Downloads the tracks not already on disk. Returns key → file path."""
        pending = plan.pending
        if not pending:
            logger.info("[playlists] Every track is already in the output directory")
            return {}

        tracks = await self._register_queue_async([p.track for p in pending])
        worker = DownloadWorker(
            tracks=tracks,
            opts=opts,
            # Single folder: no playlist subdirectory.
            collection_name="",
            is_album=False,
            is_playlist=False,
            positions=[p.position for p in pending],
        )
        await worker.run_async()

        completed = worker.completed_paths
        return {
            planned.key: Path(completed[track.id])
            for planned, track in zip(pending, tracks)
            if track.id in completed
        }

    async def _write_playlist_files_async(
        self,
        plan: SyncPlan,
        located: dict[str, Path],
        output_dir: Path,
        m3u_format: str,
    ) -> None:
        """Refreshes one M3U file per playlist, skipping the unchanged ones."""
        planned_by_key = plan.track_by_key()
        rows: list[tuple[str, str, int, int]] = []

        for playlist in plan.playlists:
            entries = [
                entry_for(planned_by_key[key].track, located[key])
                for key in playlist.keys
                if key in located
            ]
            missing = len(playlist.keys) - len(entries)

            if m3u_format == "none":
                rows.append(
                    (playlist.source.name, "no playlist file", len(entries), missing)
                )
                continue

            target = output_dir / playlist.file_name
            content = render_m3u(entries, target)
            existed = await asyncio.to_thread(target.is_file)
            changed = await write_if_changed_async(target, content)

            if changed:
                status = "updated" if existed else "created"
            else:
                status = "unchanged"
            rows.append((playlist.file_name, status, len(entries), missing))

        print_playlist_summary(rows, len(plan.tracks), len(plan.present))

    async def _record_history_async(
        self,
        url: str,
        collection_name: str,
        tracks: list[TrackMetadata],
        info: dict,
    ) -> None:
        """Adds the resolved URL to the recent-links history. Never fatal."""
        try:
            from .core.session_memory import add_url_to_history_async

            cover_url = (
                tracks[0].cover_url
                if tracks and getattr(tracks[0], "cover_url", "")
                else ""
            )
            url_type = info.get("type", "")
            if url_type == "artist_discography":
                url_type = "artist"
            artist = tracks[0].artists if tracks and url_type == "track" else ""
            await add_url_to_history_async(
                url,
                label=collection_name,
                cover=cover_url,
                track_count=len(tracks),
                url_type=url_type,
                artist=artist,
            )
        except Exception as exc:
            logger.debug("[downloader] Failed operation: %s", exc)

    async def _resolve_metadata_async(
        self,
        url: str,
    ) -> tuple[str, list[TrackMetadata], dict]:
        from .core.apple_music_metadata import is_apple_music_url, parse_apple_music_url
        from .core.tidal_metadata import is_tidal_url, parse_tidal_url

        is_tidal = is_tidal_url(url)
        is_apple = is_apple_music_url(url)
        is_soundcloud = "soundcloud.com" in url or "on.soundcloud.com" in url
        is_youtube = "youtube.com" in url or "youtu.be" in url
        is_pandora = "pandora.com" in url or "pandora.app.link" in url

        if "deezer.com" in url or "deezer.page.link" in url:
            raise SpotiflacError(
                ErrorKind.INVALID_URL,
                "Providing Deezer URLs as primary input is not yet fully supported. "
                "Use a Spotify link and set 'deezer' as the download provider.",
            )

        if "amazon." in url.lower():
            raise SpotiflacError(
                ErrorKind.INVALID_URL,
                "Amazon links cannot be inserted.",
            )

        try:
            if is_tidal:
                from .core.tidal_metadata import TidalMetadataClient

                client = TidalMetadataClient()
                collection_name, tracks, *collection_cover = (
                    await _call_metadata_get_url(
                        client, url, include_featuring=self._opts.include_featuring
                    )
                )
            elif is_apple:
                from .core.apple_music_metadata import AppleMusicMetadataClient

                client = AppleMusicMetadataClient()
                collection_name, tracks, *collection_cover = (
                    await _call_metadata_get_url(
                        client, url, include_featuring=self._opts.include_featuring
                    )
                )
            elif is_soundcloud:
                sc_providers = _build_providers_for_name("soundcloud", self._opts)
                if not sc_providers:
                    raise SpotiflacError(
                        ErrorKind.UNAVAILABLE, "SoundCloud provider not installed"
                    )
                response = await _call_metadata_get_url(sc_providers[0], url)
                collection_name, tracks, *collection_cover = (
                    _adapt_js_metadata_response(response)
                )
            elif is_youtube:
                yt_providers = _build_providers_for_name("youtube", self._opts)
                if not yt_providers:
                    raise SpotiflacError(
                        ErrorKind.UNAVAILABLE, "YouTube provider not installed"
                    )
                response = await _call_metadata_get_url(yt_providers[0], url)
                collection_name, tracks, *collection_cover = (
                    _adapt_js_metadata_response(response)
                )
            elif is_pandora:
                pd_providers = _build_providers_for_name("pandora", self._opts)
                if not pd_providers:
                    raise SpotiflacError(
                        ErrorKind.UNAVAILABLE, "Pandora provider not installed"
                    )
                response = await _call_metadata_get_url(pd_providers[0], url)
                collection_name, tracks, *collection_cover = (
                    _adapt_js_metadata_response(response)
                )
            else:
                collection_name, tracks, *_collection_cover = (
                    await _call_metadata_get_url(
                        self._metadata_client(),
                        url,
                        include_featuring=self._opts.include_featuring,
                    )
                )
        except SpotiflacError:
            raise
        except Exception as exc:
            raise SpotiflacError(
                ErrorKind.NETWORK_ERROR, f"Metadata fetch failed: {exc}", cause=exc
            )

        if not tracks:
            return collection_name, [], {}

        if is_tidal:
            info = parse_tidal_url(url)
        elif is_apple:
            info = parse_apple_music_url(url)
        elif is_soundcloud:
            from urllib.parse import urlparse as _urlparse

            _parts = [p for p in _urlparse(url).path.strip("/").split("/") if p]
            if len(_parts) >= 2 and _parts[1] == "sets":
                stype = "playlist"
            elif len(_parts) == 1:
                stype = "artist"
            else:
                stype = "track"
            info = {"type": stype, "id": url}
        elif is_youtube:
            stype = "track"
            if "list=" in url or "/playlist" in url:
                stype = "playlist"
            elif "/browse/" in url or "/channel/" in url:
                stype = "artist_discography"
            info = {"type": stype, "id": url}
        elif is_pandora:
            from urllib.parse import urlparse as _urlparse

            _parts = [p for p in _urlparse(url).path.strip("/").split("/") if p]
            stype = "track"
            if "playlist" in _parts:
                stype = "playlist"
            elif "album" in _parts:
                stype = "album"
            info = {"type": stype, "id": url}
        else:
            from .core.spotify_metadata import parse_spotify_url

            info = parse_spotify_url(url)

        if not info:
            raise SpotiflacError(
                ErrorKind.INVALID_URL, f"Unsupported or invalid URL: {url}"
            )

        return collection_name, tracks, info

    async def _resolve_isrc_bulk_async(
        self,
        tracks: list[TrackMetadata],
    ) -> list[TrackMetadata]:
        missing = [t for t in tracks if not t.isrc]
        if not missing:
            return tracks

        only_youtube = (
            len(self._opts.services) == 1 and self._opts.services[0] == "youtube"
        )

        if only_youtube:
            return tracks

        try:
            resolver = IsrcHelper(AsyncHttpClient("isrc"))

            async def _resolve_one(i: int, track: TrackMetadata):
                if track.isrc:
                    return i, track
                if hasattr(resolver, "get_isrc_async"):
                    resolved = await resolver.get_isrc_async(track.id)
                else:
                    resolved = await asyncio.to_thread(resolver.get_isrc, track.id)

                if resolved:
                    return i, track.model_copy(update={"isrc": resolved})
                return i, track

            tasks = [_resolve_one(i, t) for i, t in enumerate(tracks) if not t.isrc]
            results = await asyncio.gather(*tasks)

            for i, updated in results:
                tracks[i] = updated

        except Exception as exc:
            logger.warning("[isrc] bulk resolution async failed: %s", exc)

        # Some Spotify playlists may lose album/release dates during bulk
        # ISRC resolution. For tracks that still miss a `release_date` but
        # have an `open.spotify.com/track/` external URL, fetch detailed
        # track metadata and hydrate the release date when available.
        try:
            missing_dates = [
                (idx, t)
                for idx, t in enumerate(tracks)
                if not t.release_date
                and "open.spotify.com/track/" in (t.external_url or "")
            ]
            if missing_dates:
                semaphore = asyncio.Semaphore(10)

                async def _hydrate(idx: int, track: TrackMetadata):
                    async with semaphore:
                        try:
                            detailed = await self._metadata_client().get_track_async(
                                track.id
                            )
                        except Exception as exc:
                            logger.warning(
                                "Could not fetch release date for %s: %s",
                                track.title,
                                exc,
                            )
                            return idx, track
                    if not detailed.release_date:
                        return idx, track
                    return idx, track.model_copy(
                        update={"release_date": detailed.release_date}
                    )

                for i, updated in await asyncio.gather(
                    *(_hydrate(i, t) for i, t in missing_dates)
                ):
                    tracks[i] = updated
        except Exception:
            # Non-fatal — keep original tracks if hydration fails
            pass

        return tracks

    async def _register_queue_async(
        self,
        tracks: list[TrackMetadata],
    ) -> list[TrackMetadata]:
        """Adds the tracks to the download queue, giving an id to those without.

        Returns the tracks with their final ids: everything downstream (progress
        updates, per-track results) is keyed on them.
        """
        manager = DownloadManager()
        updated_tracks = []
        for i, t in enumerate(tracks):
            track_item_id = t.id or t.external_url or f"queue-{i}-{uuid.uuid4().hex}"
            track_spotify_id = t.id or t.external_url or track_item_id
            await manager.add_to_queue(
                track_item_id,
                t.title,
                t.artists,
                t.album,
                track_spotify_id,
            )
            if not t.id:
                t = t.model_copy(update={"id": track_item_id})
            updated_tracks.append(t)
        return updated_tracks

    async def _run_worker_async(
        self,
        tracks: list[TrackMetadata],
        collection_name: str,
        info: dict,
        is_album: bool,
        is_playlist: bool,
        opts: DownloadOptions | None = None,
    ) -> list[TrackMetadata]:
        effective = opts if opts is not None else self._opts
        updated_tracks = await self._register_queue_async(tracks)

        worker = DownloadWorker(
            tracks=updated_tracks,
            opts=effective,
            collection_name=collection_name,
            is_album=is_album,
            is_playlist=is_playlist,
        )

        failed_tuples = await worker.run_async()
        failed_ids = {f[0] for f in failed_tuples}
        return [t for t in updated_tracks if t.id in failed_ids]

    async def _run_once_async(
        self,
        url: str,
        target_tracks=None,
    ) -> list[TrackMetadata]:
        if target_tracks is not None:
            tracks = target_tracks
            collection_name = "Retry Failed Tracks"
            is_album = self._opts.is_album
            is_playlist = len(tracks) > 1
            return await self._run_worker_async(
                tracks,
                collection_name,
                {},
                is_album,
                is_playlist,
            )

        try:
            collection_name, tracks, info = await self._resolve_metadata_async(url)
        except SpotiflacError as exc:
            logger.exception("Metadata fetch failed: %s", exc)
            return []

        if not tracks:
            return []

        is_album = info.get("type") == "album"
        is_playlist = info.get("type") == "playlist"
        is_discography = info.get("type") in ("artist", "artist_discography")

        effective_opts = self._opts
        if self._opts.is_album != is_album:
            effective_opts = replace(self._opts, is_album=is_album)

        if (is_album or is_playlist or is_discography) and self._opts.output_path:
            logger.warning(
                "[downloader] --output-path ignored for %s: "
                "files will be saved with standard renaming.",
                info.get("type"),
            )
            effective_opts = replace(effective_opts, output_path=None)

        is_soundcloud = "soundcloud.com" in url or "on.soundcloud.com" in url
        is_pandora = "pandora.com" in url or "pandora.app.link" in url

        if not is_soundcloud and not is_pandora:
            tracks = await self._resolve_isrc_bulk_async(tracks)

        await self._record_history_async(url, collection_name, tracks, info)

        return await self._run_worker_async(
            tracks,
            collection_name,
            info,
            is_album,
            is_playlist,
            opts=effective_opts,
        )

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        s = round(seconds)
        parts = []
        for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
            val, s = divmod(s, div)
            if val:
                parts.append(f"{val}{unit}")
        return " ".join(parts) or "0s"

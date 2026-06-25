"""
Downloader — main orchestrator (100% Async Native).
Changes compared to the original:
  - DownloadOptions: +track_max_retries, +post_download_action, +post_download_command
  - download_one_async(): per-track retry with exponential backoff and pure async flow
  - DownloadWorker.run_async(): async semaphores for concurrent task orchestration
  - SpotiflacDownloader.run_async(): fully async batch processing and metadata fetching
  - 100% Asynchronous I/O wrappers for filesystem operations
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field

from .core.console import print_track_header, print_summary
from .core.errors import SpotiflacError, ErrorKind
from .core.models import TrackMetadata, DownloadResult
from .core.progress import DownloadManager, ProgressManager, ProgressCallback, safe_tqdm_write, install_console_interception, uninstall_console_interception
from .providers.base import BaseProvider
from .providers.spotify_metadata import SpotifyMetadataClient
from .core.isrc_helper import IsrcHelper
from .core.http import AsyncHttpClient
from .core.quality import normalize_quality

logger = logging.getLogger(__name__)


@dataclass
class DownloadOptions:
    output_dir:              str
    services:                list[str]       = field(default_factory=lambda: ["tidal"])
    filename_format:         str             = "{title} - {artist}"
    use_track_numbers:       bool            = False
    use_album_track_numbers: bool            = False
    use_artist_subfolders:   bool            = False
    use_album_subfolders:    bool            = False
    first_artist_only:       bool            = False
    quality:                 str             = "LOSSLESS"
    allow_fallback:          bool            = True
    inter_track_delay_s:     float           = 1.0
    is_album:                bool            = False
    output_path:             str | None      = None

    embed_lyrics:            bool            = True
    lyrics_providers:        list[str]       = field(
        default_factory=lambda: ["spotify", "apple", "musixmatch", "lrclib", "amazon"]
    )

    enrich_metadata:         bool            = True
    enrich_providers:        list[str]       = field(
        default_factory=lambda: ["deezer", "apple", "qobuz", "tidal", "soundcloud"]
    )
    qobuz_token:             str | None      = None
    qobuz_local_api_url:     str | None      = None

    track_max_retries:       int             = 0
    post_download_action:    str             = "none"
    post_download_command:   str             = ""
    tidal_custom_api:        str | None      = None
    timeout_s:               int | None      = None


def _build_provider(name: str, opts: DownloadOptions) -> BaseProvider | None:
    from .providers import PROVIDER_REGISTRY
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        logger.warning("Unknown provider: %s", name)
        return None
    kwargs = {}
    if opts.timeout_s is not None:
        kwargs["timeout_s"] = opts.timeout_s

    if name == "qobuz":
        kwargs["qobuz_token"] = opts.qobuz_token
        kwargs["local_api_url"] = opts.qobuz_local_api_url
    elif name == "tidal" and opts.tidal_custom_api:
        kwargs["custom_api_url"] = opts.tidal_custom_api

    return cls(**kwargs)


async def _move_file_async(src: str, dst: str) -> None:
    """Helper asincrono thread-safe per rinominare/spostare file."""
    def _do_move():
        os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dst):
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
    await asyncio.to_thread(_do_move)


async def _get_file_size_mb_async(path: str) -> float:
    """Helper asincrono thread-safe per calcolare il peso in MB."""
    def _do_get():
        if path and os.path.exists(path):
            return os.path.getsize(path) / (1024 * 1024)
        return 0.0
    return await asyncio.to_thread(_do_get)


async def download_one_async(
        metadata:   TrackMetadata,
        output_dir: str,
        providers:  list[BaseProvider],
        opts:       DownloadOptions,
        position:   int = 1,
        is_album:   bool = False,
) -> DownloadResult:
    """
    Attempts to download a single track across all providers in order,
    with per-track retry if track_max_retries > 0.
    """
    stop_event = asyncio.Event()
    DownloadManager()
    errors: dict[str, str] = {}
    started_at = time.monotonic()

    for attempt in range(opts.track_max_retries + 1):
        if stop_event.is_set() or (opts.timeout_s and time.monotonic() - started_at >= opts.timeout_s):
            return DownloadResult.fail("none", f"Download timed out after {opts.timeout_s}s")

        if attempt > 0:
            wait = min(2 ** attempt, 30)
            safe_tqdm_write(f"\n  ↺  Retry {attempt}/{opts.track_max_retries} in {wait}s…")
            await asyncio.sleep(wait)
            errors.clear()

        for provider in providers:
            logger.info("[%s] Trying: %s — %s", provider.name, metadata.artists, metadata.title)
            cb = ProgressCallback(item_id=metadata.id, track_name=metadata.title)
            provider.set_progress_callback(cb)
            
            # Cooperative shutdown propagation
            if hasattr(provider, "set_stop_event_async"):
                try:
                    provider.set_stop_event_async(stop_event)
                except Exception:
                    pass

            try:
                # Wrap inside asyncio.wait_for to enforce track timeout strictly at the IO level
                time_elapsed = time.monotonic() - started_at
                timeout_left = max(1, opts.timeout_s - time_elapsed) if opts.timeout_s else None
                
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
                logger.warning("[downloader] timeout exceeded for track '%s'", metadata.title)
                safe_tqdm_write(f"\n  ⏱  Timeout reached for '{metadata.title}' — skipping track.")
                return DownloadResult.fail("none", f"Download timed out after {opts.timeout_s}s")

            if result.success:
                if result.skipped:
                    logger.info("[%s] ⏭ %s — %s", provider.name, metadata.artists, metadata.title)
                    return result
                if opts.output_path and result.file_path:
                    _, ext = os.path.splitext(result.file_path)
                    base_target, _ = os.path.splitext(opts.output_path)
                    target = base_target + ext
                    # Spostamento delegato all'I/O asincrono
                    await _move_file_async(result.file_path, target)
                    result = DownloadResult.ok(result.provider, target, result.format or "flac")

                logger.info("[%s] ✓ %s — %s", provider.name, metadata.artists, metadata.title)
                return result

            errors[provider.name] = result.error or "unknown error"
            safe_tqdm_write(f"  ✗  {provider.name}  ·  {result.error}", file=sys.stderr)
            logger.debug("[%s] ✗ %s", provider.name, result.error)

    attempts_str = f"{opts.track_max_retries + 1} attempt(s)"
    summary = "; ".join(f"{k}: {v}" for k, v in errors.items())
    return DownloadResult.fail("none", f"All providers failed after {attempts_str} — {summary}")


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
            print(f"\n  🔔 {title}: {body}")
        else:
            await asyncio.create_subprocess_exec("notify-send", title, body)
    except Exception:
        print(f"\n  🔔 {title}: {body}")


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
            tracks:          list[TrackMetadata],
            opts:            DownloadOptions,
            collection_name: str  = "",
            is_album:        bool = False,
            is_playlist:     bool = False,
    ) -> None:
        self._tracks          = tracks
        self._opts            = opts
        self._collection_name = collection_name
        self._is_album        = is_album
        self._is_playlist     = is_playlist
        self._failed:  list[tuple[str, str, str, str]] = []
        self._providers: list[BaseProvider] = self._build_providers()

    def _build_providers(self) -> list[BaseProvider]:
        result = []
        for name in self._opts.services:
            p = _build_provider(name, self._opts)
            if p:
                result.append(p)
        if not result:
            raise ValueError(f"No valid providers found in: {self._opts.services}")
        return result

    async def run_async(self) -> list[tuple[str, str, str]]:
        manager   = DownloadManager()
        await manager.reset()
        total     = len(self._tracks)
        start     = time.perf_counter()
        
        # Delegazione I/O cartelle nativa asincrona
        base_out  = await self._resolve_output_dir_async()

        install_console_interception()
        ProgressManager.initialize_master_bar(total, description="Progress")
        try:
            return await self._run_downloads_async(manager, total, base_out, start)
        finally:
            await ProgressManager.clear_all()
            uninstall_console_interception()

    async def _run_downloads_async(
        self,
        manager:  DownloadManager,
        total:    int,
        base_out: str,
        start:    float,
    ) -> list[tuple[str, str, str]]:
        MAX_CONCURRENT_DOWNLOADS = 4
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

        async def worker_task(i: int, track: TrackMetadata):
            position = i + 1
            print_track_header(position, total, track.title, track.artists, track.album)
            await manager.start_download(track.id)

            out_dir = await self._track_output_dir_async(base_out, track)
            result = await download_one_async(
                track,
                out_dir,
                self._providers,
                self._opts,
                position,
                self._is_album,
            )
            return track, result

        async def limited_worker(i: int, track: TrackMetadata):
            async with semaphore:
                return await worker_task(i, track)

        tasks = [asyncio.create_task(limited_worker(i, track)) for i, track in enumerate(self._tracks)]
        
        for coro in asyncio.as_completed(tasks):
            track, result = await coro
            if result.success and result.skipped:
                await manager.skip_download(track.id)
            elif result.success:
                size_mb = await _get_file_size_mb_async(result.file_path)
                await manager.complete_download(track.id, result.file_path or "", size_mb)
            else:
                err = result.error or "unknown"
                self._failed.append((track.id, track.title, track.artists, err))
                safe_tqdm_write(f"\n  ✗  Failed: {track.title} — {track.artists}: {err}", file=sys.stderr)
                logger.debug("[worker] Failed: %s — %s: %s", track.title, track.artists, err)
                await manager.fail_download(track.id, err)
                from .core.progress import ProgressCallback
                ProgressCallback.clear_item(track.id)

            ProgressManager.increment_master()

        elapsed = time.perf_counter() - start
        self._print_summary(elapsed)
        await self._execute_post_action_async(base_out)
        return self._failed

    async def _resolve_output_dir_async(self) -> str:
        """Risolve asincronamente la directory di output assicurandosi che esista."""
        def _do_resolve():
            if self._opts.output_path:
                out = os.path.normpath(
                    os.path.dirname(os.path.abspath(self._opts.output_path))
                )
                os.makedirs(out, exist_ok=True)
                return out

            out = os.path.normpath(self._opts.output_dir)
            if self._is_playlist and self._collection_name:
                safe_name = re.sub(r'[<>:"/\\|?*]', "_", self._collection_name.strip())
                out = os.path.join(out, safe_name)
            elif self._is_album and self._collection_name and not self._opts.use_album_subfolders:
                safe_name = re.sub(r'[<>:"/\\|?*]', "_", self._collection_name.strip())
                out = os.path.join(out, safe_name)
            
            os.makedirs(out, exist_ok=True)
            return out
        return await asyncio.to_thread(_do_resolve)

    async def _track_output_dir_async(self, base: str, track: TrackMetadata) -> str:
        """Crea asincronamente eventuali sottocartelle basate sull'artista o sull'album."""
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
        succeeded = len(self._tracks) - len(self._failed)
        display = [(t, a, e) for _, t, a, e in self._failed]
        print_summary(len(self._tracks), succeeded, display, elapsed)

    async def _execute_post_action_async(self, output_dir: str) -> None:
        action = self._opts.post_download_action
        if not action or action == "none":
            return

        succeeded   = len(self._tracks) - len(self._failed)
        failed_count = len(self._failed)

        if action == "open_folder":
            print(f"\n  📂 Opening folder: {output_dir}")
            await _open_folder_async(output_dir)

        elif action == "notify":
            body = f"{succeeded} tracks downloaded"
            if failed_count:
                body += f", {failed_count} failed"
            await _send_system_notify_async("SpotiFLAC — Download completed", body)

        elif action == "command":
            cmd_template = self._opts.post_download_command
            if not cmd_template:
                logger.warning("[post-action] action=command but post_download_command is empty")
                return
            cmd = (
                cmd_template
                .replace("{folder}",    output_dir)
                .replace("{succeeded}", str(succeeded))
                .replace("{failed}",    str(failed_count))
            )
            try:
                print(f"\n  ▶  Executing post-download command: {cmd[:80]}")
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
        self._opts   = opts
        self._client = SpotifyMetadataClient()

    async def run_async(self, input_url: str | list[str], loop_minutes: int | None = None) -> None:
        """
        Starts downloading one or more URLs using the async worker pipeline.
        """
        urls = [input_url] if isinstance(input_url, str) else list(input_url)

        for idx, url in enumerate(urls):
            if len(urls) > 1:
                print(f"\n{'═' * 55}")
                print(f"  URL {idx + 1}/{len(urls)}: {url[:55]}")
                print(f"{'═' * 55}")

            failed_tracks = None
            while True:
                failed_tracks = await self._run_once_async(url, target_tracks=failed_tracks)
                if not loop_minutes or loop_minutes <= 0 or not failed_tracks:
                    break
                print(f"\n{len(failed_tracks)} tracks failed. "
                      f"Next attempt in {loop_minutes} minutes…")
                await asyncio.sleep(loop_minutes * 60)

    async def _resolve_metadata_async(self, url: str) -> tuple[str, list[TrackMetadata], dict]:
        from .providers.tidal_metadata import is_tidal_url, parse_tidal_url
        from .providers.apple_music_metadata import is_apple_music_url, parse_apple_music_url
        from .providers.pandora import is_pandora_url, parse_pandora_url

        print("Fetching metadata…")

        is_tidal      = is_tidal_url(url)
        is_apple      = is_apple_music_url(url)
        is_soundcloud = "soundcloud.com" in url or "on.soundcloud.com" in url
        is_youtube    = "youtube.com" in url or "youtu.be" in url
        is_pandora    = is_pandora_url(url)

        if "deezer.com" in url or "deezer.page.link" in url:
            raise SpotiflacError(
                ErrorKind.INVALID_URL,
                "Providing Deezer URLs as primary input is not yet fully supported. "
                "Use a Spotify link and set 'deezer' as the download provider."
            )
        
        if "amazon." in url.lower():
            raise SpotiflacError(
                ErrorKind.INVALID_URL,
                "Amazon links cannot be inserted."
            )

        try:
            if is_tidal:
                from .providers.tidal_metadata import TidalMetadataClient
                client = TidalMetadataClient()
                if hasattr(client, 'get_url_async'):
                    collection_name, tracks, *collection_cover = await client.get_url_async(url, include_featuring=self._opts.include_featuring)
                else:
                    collection_name, tracks, *collection_cover = await asyncio.to_thread(client.get_url, url, include_featuring=self._opts.include_featuring)
            elif is_apple:
                from .providers.apple_music_metadata import AppleMusicMetadataClient
                client = AppleMusicMetadataClient()
                if hasattr(client, 'get_url_async'):
                    collection_name, tracks, *collection_cover = await client.get_url_async(url)
                else:
                    collection_name, tracks, *collection_cover = await asyncio.to_thread(client.get_url, url)
            elif is_soundcloud:
                from .providers.soundcloud import SoundCloudProvider
                client = SoundCloudProvider()
                if hasattr(client, 'get_url_async'):
                    collection_name, tracks, *collection_cover = await client.get_url_async(url)
                else:
                    collection_name, tracks, *collection_cover = await asyncio.to_thread(client.get_url, url)
            elif is_youtube:
                from .providers.youtube import YouTubeProvider
                client = YouTubeProvider()
                if hasattr(client, 'get_url_async'):
                    collection_name, tracks, *collection_cover = await client.get_url_async(url)
                else:
                    collection_name, tracks, *collection_cover = await asyncio.to_thread(client.get_url, url)
            elif is_pandora:
                from .providers.pandora import PandoraProvider
                client = PandoraProvider()
                if hasattr(client, 'get_url_async'):
                    collection_name, tracks, *collection_cover = await client.get_url_async(url)
                else:
                    collection_name, tracks, *collection_cover = await asyncio.to_thread(client.get_url, url)
            else:
                if hasattr(self._client, 'get_url_async'):
                    collection_name, tracks, *collection_cover = await self._client.get_url_async(url)
                else:
                    collection_name, tracks, *collection_cover = await asyncio.to_thread(self._client.get_url, url)
        except SpotiflacError:
            raise
        except Exception as exc:
            raise SpotiflacError(ErrorKind.NETWORK_ERROR, f"Metadata fetch failed: {exc}", cause=exc)

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
            info = parse_pandora_url(url)
        else:
            from .providers.spotify_metadata import parse_spotify_url
            info = parse_spotify_url(url)

        if not info:
            raise SpotiflacError(ErrorKind.INVALID_URL, f"Unsupported or invalid URL: {url}")

        print(f"Found {len(tracks)} track(s) in: {collection_name}")
        return collection_name, tracks, info

    async def _resolve_isrc_bulk_async(self, tracks: list[TrackMetadata]) -> list[TrackMetadata]:
        missing = [t for t in tracks if not t.isrc]
        if not missing:
            return tracks

        only_youtube = len(self._opts.services) == 1 and self._opts.services[0] == "youtube"

        if only_youtube:
            return tracks

        print(f"Resolving ISRC for {len(missing)} track(s)…")
        try:
            resolver = IsrcHelper(AsyncHttpClient("isrc"))

            async def _resolve_one(i: int, track: TrackMetadata):
                if track.isrc:
                    return i, track
                if hasattr(resolver, 'get_isrc_async'):
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

        return tracks

    async def _run_worker_async(
            self,
            tracks:          list[TrackMetadata],
            collection_name: str,
            info:            dict,
            is_album:        bool,
            is_playlist:     bool,
            opts:            DownloadOptions | None = None,
    ) -> list[TrackMetadata]:
        effective = opts if opts is not None else self._opts
        manager = DownloadManager()
        updated_tracks = []
        for i, t in enumerate(tracks):
            track_item_id = t.id or t.external_url or f"queue-{i}-{uuid.uuid4().hex}"
            track_spotify_id = t.id or t.external_url or track_item_id
            await manager.add_to_queue(track_item_id, t.title, t.artists, t.album, track_spotify_id)
            if not t.id:
                t = t.model_copy(update={"id": track_item_id})
            updated_tracks.append(t)

        worker = DownloadWorker(
            tracks          = updated_tracks,
            opts            = effective,
            collection_name = collection_name,
            is_album        = is_album,
            is_playlist     = is_playlist,
        )

        failed_tuples = await worker.run_async()
        failed_ids = {f[0] for f in failed_tuples}
        return [t for t in updated_tracks if t.id in failed_ids]

    async def _run_once_async(self, url: str, target_tracks=None) -> list[TrackMetadata]:
        if target_tracks is not None:
            print(f"\nRetrying download for {len(target_tracks)} track(s)...")
            tracks          = target_tracks
            collection_name = "Retry Failed Tracks"
            is_album        = self._opts.is_album
            is_playlist     = len(tracks) > 1
            return await self._run_worker_async(tracks, collection_name, {}, is_album, is_playlist)

        try:
            collection_name, tracks, info = await self._resolve_metadata_async(url)
        except SpotiflacError as exc:
            logger.error("Metadata fetch failed: %s", exc)
            print(f"Error: {exc}")
            return []

        if not tracks:
            print("No tracks found.")
            return []

        is_album       = info.get("type") == "album"
        is_playlist    = info.get("type") == "playlist"
        is_discography = info.get("type") in ("artist", "artist_discography")

        effective_opts = self._opts
        if self._opts.is_album != is_album:
            from dataclasses import replace
            effective_opts = replace(self._opts, is_album=is_album)

        if (is_album or is_playlist or is_discography) and self._opts.output_path:
            logger.warning(
                "[downloader] --output-path ignored for %s: "
                "files will be saved with standard renaming.",
                info.get("type"),
            )
            from dataclasses import replace
            effective_opts = replace(effective_opts, output_path=None)

        is_soundcloud = "soundcloud.com" in url or "on.soundcloud.com" in url
        is_pandora    = "pandora.com" in url or "pandora.app.link" in url

        if not is_soundcloud and not is_pandora:
            tracks = await self._resolve_isrc_bulk_async(tracks)

        try:
            from .core.session_memory import add_url_to_history_async
            cover_url = tracks[0].cover_url if tracks and getattr(tracks[0], 'cover_url', '') else ''
            _url_type = info.get("type", "")
            if _url_type == "artist_discography":
                _url_type = "artist"
            _artist = tracks[0].artists if tracks and _url_type == 'track' else ''
            await add_url_to_history_async(url, label=collection_name, cover=cover_url,
                                           track_count=len(tracks), url_type=_url_type, artist=_artist)
        except Exception as exc:
            logger.debug("[downloader] Failed operation: %s", exc)
            
        return await self._run_worker_async(tracks, collection_name, info, is_album, is_playlist, opts=effective_opts)

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        s = int(round(seconds))
        parts = []
        for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
            val, s = divmod(s, div)
            if val:
                parts.append(f"{val}{unit}")
        return " ".join(parts) or "0s"

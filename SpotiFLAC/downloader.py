"""
Downloader — main orchestrator.
Changes compared to the original:
  - DownloadOptions: +track_max_retries, +post_download_action, +post_download_command
  - download_one(): per-track retry with exponential backoff
  - DownloadWorker: post-download actions (open_folder / notify / command)
  - SpotiflacDownloader.run(): accepts str | list[str] for batch mode
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .core.console import print_track_header, print_summary
from .core.errors import SpotiflacError, ErrorKind
from .core.models import TrackMetadata, DownloadResult
from .core.progress import DownloadManager, ProgressCallback
from .providers.base import BaseProvider
from .providers.spotify_metadata import SpotifyMetadataClient
from .core.isrc_helper import IsrcHelper
from .core.http import HttpClient
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    inter_track_delay_s:     float           = 0.5
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
    include_featuring:       bool            = False

    # ── New fields ───────────────────────────────────────────────────────
    track_max_retries:       int             = 0
    # none | open_folder | notify | command
    post_download_action:    str             = "none"
    # Shell command (used when action == "command")
    # Supported placeholders: {folder} {succeeded} {failed}
    post_download_command:   str             = ""


def _build_provider(name: str, opts: DownloadOptions) -> BaseProvider | None:
    from .providers import PROVIDER_REGISTRY
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        logger.warning("Unknown provider: %s", name)
        return None
    kwargs = {"qobuz_token": opts.qobuz_token} if name in ("tidal", "qobuz") else {}
    return cls(**kwargs)


def download_one(
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

    Retry strategy: exponential backoff (2^attempt seconds, max 30s).
    Each retry starts over from the first provider in the list.
    """
    max_retries = opts.track_max_retries
    manager = DownloadManager()
    errors: dict[str, str] = {}

    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = min(2 ** attempt, 30)
            print(f"\n  ↺  Retry {attempt}/{max_retries} in {wait}s… "
                  f"({metadata.artists} — {metadata.title})")
            time.sleep(wait)
            errors.clear()

        for provider in providers:
            logger.info("[%s] Trying: %s — %s", provider.name, metadata.artists, metadata.title)

            cb = ProgressCallback(item_id=metadata.id)
            provider.set_progress_callback(cb)

            result = provider.download_track(
                metadata,
                output_dir,
                filename_format         = opts.filename_format,
                position                = position,
                include_track_num       = opts.use_track_numbers,
                use_album_track_num     = opts.use_album_track_numbers,
                first_artist_only       = opts.first_artist_only,
                allow_fallback          = opts.allow_fallback,
                quality                 = opts.quality,
                embed_lyrics            = opts.embed_lyrics,
                lyrics_providers        = opts.lyrics_providers,
                enrich_metadata         = opts.enrich_metadata,
                enrich_providers        = opts.enrich_providers,
                qobuz_token             = opts.qobuz_token,
                is_album                = is_album,
            )

            if result.success:
                if opts.output_path and result.file_path:
                    import shutil
                    _, ext = os.path.splitext(result.file_path)
                    base_target, _ = os.path.splitext(opts.output_path)
                    target = base_target + ext
                    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
                    if os.path.abspath(result.file_path) != os.path.abspath(target):
                        if os.path.exists(target):
                            os.remove(target)
                        shutil.move(result.file_path, target)
                    result = DownloadResult.ok(result.provider, target, result.format or "flac")

                logger.info("[%s] ✓ %s — %s", provider.name, metadata.artists, metadata.title)
                return result

            errors[provider.name] = result.error or "unknown error"
            logger.warning("[%s] ✗ %s", provider.name, result.error)

    attempts_str = f"{max_retries + 1} attempt(s)"
    summary = "; ".join(f"{k}: {v}" for k, v in errors.items())
    return DownloadResult.fail("none", f"All providers failed after {attempts_str} — {summary}")


# ---------------------------------------------------------------------------
# Post-download actions helpers
# ---------------------------------------------------------------------------

def _send_system_notify(title: str, body: str) -> None:
    """Sends a system notification (Linux/macOS/Windows)."""
    try:
        if sys.platform == "darwin":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], timeout=3, check=False)
        elif sys.platform == "win32":
            # Windows: text fallback (avoids extra dependencies)
            print(f"\n  🔔 {title}: {body}")
        else:
            subprocess.run(["notify-send", title, body], timeout=3, check=False)
    except Exception:
        print(f"\n  🔔 {title}: {body}")


def _open_folder(path: str) -> None:
    """Opens the folder in the system file manager."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", path])
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

    def run(self) -> list[tuple[str, str, str]]:
        manager   = DownloadManager()
        manager.reset()
        total     = len(self._tracks)
        start     = time.perf_counter()
        base_out  = self._resolve_output_dir()

        for i, track in enumerate(self._tracks):
            position = i + 1
            print_track_header(position, total, track.title, track.artists, track.album)

            manager.start_download(track.id)

            out_dir = self._track_output_dir(base_out, track)
            result  = download_one(
                track, out_dir, self._providers, self._opts, position, self._is_album
            )

            if result.success:
                size_mb = (
                    os.path.getsize(result.file_path) / (1024 * 1024)
                    if result.file_path and os.path.exists(result.file_path)
                    else 0.0
                )
                manager.complete_download(track.id, result.file_path or "", size_mb)
            else:
                err = result.error or "unknown"
                self._failed.append((track.id, track.title, track.artists, err))
                logger.error("[worker] Failed: %s — %s: %s", track.title, track.artists, err)
                manager.fail_download(track.id, err)

            if i < total - 1:
                time.sleep(self._opts.inter_track_delay_s)

        elapsed = time.perf_counter() - start
        self._print_summary(elapsed)

        # ── Post-download action ───────────────────────────────────────────
        self._execute_post_action(base_out)

        return self._failed

    def _resolve_output_dir(self) -> str:
        if self._opts.output_path:
            out = os.path.normpath(
                os.path.dirname(os.path.abspath(self._opts.output_path))
            )
            os.makedirs(out, exist_ok=True)
            return out

        out = os.path.normpath(self._opts.output_dir)
        if (self._is_album or self._is_playlist) and self._collection_name:
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", self._collection_name.strip())
            out = os.path.join(out, safe_name)
        os.makedirs(out, exist_ok=True)
        return out

    def _track_output_dir(self, base: str, track: TrackMetadata) -> str:
        out = base
        if self._is_playlist:
            if self._opts.use_artist_subfolders:
                folder = re.sub(r'[<>:"/\\|?*]', "_", track.first_artist)
                out = os.path.join(out, folder)
            if self._opts.use_album_subfolders:
                folder = re.sub(r'[<>:"/\\|?*]', "_", track.album)
                out = os.path.join(out, folder)
        os.makedirs(out, exist_ok=True)
        return out

    def _print_summary(self, elapsed: float) -> None:
        succeeded = len(self._tracks) - len(self._failed)
        display = [(t, a, e) for _, t, a, e in self._failed]
        print_summary(len(self._tracks), succeeded, display, elapsed)

    def _execute_post_action(self, output_dir: str) -> None:
        """
        Executes the configured post-download action.
        Supports: none | open_folder | notify | command
        """
        action = self._opts.post_download_action
        if not action or action == "none":
            return

        succeeded   = len(self._tracks) - len(self._failed)
        failed_count = len(self._failed)

        if action == "open_folder":
            print(f"\n  📂 Opening folder: {output_dir}")
            _open_folder(output_dir)

        elif action == "notify":
            body = f"{succeeded} tracks downloaded"
            if failed_count:
                body += f", {failed_count} failed"
            _send_system_notify("SpotiFLAC — Download completed", body)

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
                subprocess.Popen(cmd, shell=True)
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

    def run(self, input_url: str | list[str], loop_minutes: int | None = None) -> None:
        """
        Starts downloading one or more URLs.
        Accepts both a single string and a list of URLs (batch mode).
        """
        urls = [input_url] if isinstance(input_url, str) else list(input_url)

        for idx, url in enumerate(urls):
            if len(urls) > 1:
                print(f"\n{'═' * 55}")
                print(f"  URL {idx + 1}/{len(urls)}: {url[:55]}")
                print(f"{'═' * 55}")

            failed_tracks = None
            while True:
                failed_tracks = self._run_once(url, target_tracks=failed_tracks)
                if not loop_minutes or loop_minutes <= 0 or not failed_tracks:
                    break
                print(f"\n{len(failed_tracks)} tracks failed. "
                      f"Next attempt in {loop_minutes} minutes…")
                time.sleep(loop_minutes * 60)

    def _resolve_metadata(self, url: str) -> tuple[str, list[TrackMetadata], dict]:
        from .providers.tidal_metadata import is_tidal_url, parse_tidal_url
        from .providers.apple_music_metadata import is_apple_music_url, parse_apple_music_url

        print("Fetching metadata…")

        is_tidal      = is_tidal_url(url)
        is_apple      = is_apple_music_url(url)
        is_soundcloud = "soundcloud.com" in url or "on.soundcloud.com" in url
        is_youtube    = "youtube.com" in url or "youtu.be" in url

        if "deezer.com" in url or "deezer.page.link" in url:
            raise SpotiflacError(
                ErrorKind.INVALID_URL,
                "Providing Deezer URLs as primary input is not yet fully supported. "
                "Use a Spotify link and set 'deezer' as the download provider."
            )

        try:
            if is_tidal:
                from .providers.tidal_metadata import TidalMetadataClient
                client = TidalMetadataClient()
                collection_name, tracks = client.get_url(
                    url, include_featuring=self._opts.include_featuring
                )
            elif is_apple:
                from .providers.apple_music_metadata import AppleMusicMetadataClient
                client = AppleMusicMetadataClient()
                collection_name, tracks = client.get_url(
                    url, include_featuring=self._opts.include_featuring
                )
            elif is_soundcloud:
                from .providers.soundcloud import SoundCloudProvider
                client = SoundCloudProvider()
                collection_name, tracks = client.get_url(url)
            elif is_youtube:
                from .providers.youtube import YouTubeProvider
                client = YouTubeProvider()
                collection_name, tracks = client.get_url(url)
            else:
                collection_name, tracks = self._client.get_url(
                    url, include_featuring=self._opts.include_featuring
                )
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
        else:
            from .providers.spotify_metadata import parse_spotify_url
            info = parse_spotify_url(url)

        if not info:
            raise SpotiflacError(ErrorKind.INVALID_URL, f"Unsupported or invalid URL: {url}")

        print(f"Found {len(tracks)} track(s) in: {collection_name}")
        return collection_name, tracks, info

    def _resolve_isrc_bulk(self, tracks: list[TrackMetadata]) -> list[TrackMetadata]:
        missing = [t for t in tracks if not t.isrc]
        if not missing:
            return tracks

        only_apple   = len(self._opts.services) == 1 and self._opts.services[0] == "apple"
        only_youtube = len(self._opts.services) == 1 and self._opts.services[0] == "youtube"

        if only_apple or only_youtube:
            return tracks

        print(f"Resolving ISRC for {len(missing)} track(s)…")
        try:
            resolver = IsrcHelper(HttpClient("isrc"))

            def _resolve_one(args):
                i, track, resolver = args
                if track.isrc:
                    return i, track
                resolved = resolver.get_isrc(track.id)
                if resolved:
                    return i, track.model_copy(update={"isrc": resolved})
                return i, track

            with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
                futs = {pool.submit(_resolve_one, (i, t, resolver)): i
                        for i, t in enumerate(tracks) if not t.isrc}
                for fut in as_completed(futs):
                    try:
                        i, updated = fut.result()
                        tracks[i] = updated
                    except Exception as exc:
                        logger.debug("[isrc] resolve failed: %s", exc)
        except Exception as exc:
            logger.warning("[isrc] bulk resolution failed: %s", exc)

        return tracks

    def _run_worker(
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
        for t in tracks:
            manager.add_to_queue(t.id, t.title, t.artists, t.album, t.id)

        worker = DownloadWorker(
            tracks          = tracks,
            opts            = effective,
            collection_name = collection_name,
            is_album        = is_album,
            is_playlist     = is_playlist,
        )

        failed_tuples = worker.run()
        failed_ids = {f[0] for f in failed_tuples}
        return [t for t in tracks if t.id in failed_ids]

    def _run_once(self, url: str, target_tracks=None) -> list:
        if target_tracks is not None:
            print(f"\nRetrying download for {len(target_tracks)} track(s)...")
            tracks          = target_tracks
            collection_name = "Retry Failed Tracks"
            is_album        = self._opts.is_album
            is_playlist     = len(tracks) > 1
            return self._run_worker(tracks, collection_name, {}, is_album, is_playlist)

        try:
            collection_name, tracks, info = self._resolve_metadata(url)
        except SpotiflacError as exc:
            logger.error("Metadata fetch failed: %s", exc)
            print(f"Error: {exc}")
            return []

        if not tracks:
            print("No tracks found.")
            return []

        is_album       = info.get("type") == "album"
        is_playlist    = info.get("type") in ("playlist", "artist", "artist_discography")
        is_discography = info.get("type") in ("artist", "artist_discography")
        if is_discography:
            is_playlist = True

        effective_opts = self._opts
        if self._opts.is_album != is_album:
            from dataclasses import replace
            effective_opts = replace(self._opts, is_album=is_album)

        if (is_album or is_playlist) and self._opts.output_path:
            logger.warning(
                "[downloader] --output-path ignored for %s: "
                "files will be saved with standard renaming.",
                info.get("type"),
            )
            from dataclasses import replace
            effective_opts = replace(effective_opts, output_path=None)

        is_soundcloud = "soundcloud.com" in url or "on.soundcloud.com" in url
        if not is_soundcloud:
            tracks = self._resolve_isrc_bulk(tracks)

        # Update URL history with collection name
        try:
            from .core.session_memory import add_url_to_history
            add_url_to_history(url, label=collection_name)
        except Exception as exc:
            logger.debug("[downloader] Failed operation: %s", exc)

        return self._run_worker(tracks, collection_name, info, is_album, is_playlist, opts=effective_opts)

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        s = int(round(seconds))
        parts = []
        for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
            val, s = divmod(s, div)
            if val:
                parts.append(f"{val}{unit}")
        return " ".join(parts) or "0s"
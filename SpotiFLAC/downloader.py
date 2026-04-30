"""
Downloader — orchestratore principale.
"""
from __future__ import annotations
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core.models import TrackMetadata, DownloadResult
from .core.progress import DownloadManager, ProgressCallback
from .core.errors import SpotiflacError
from .providers.base import BaseProvider
from .providers.spotify_metadata import SpotifyMetadataClient
from .core.console import print_track_header, print_summary

logger = logging.getLogger(__name__)


@dataclass
class DownloadOptions:
    output_dir:              str
    services:                list[str]       = field(default_factory=lambda: ["tidal"])
    filename_format:         str             = "{title} - {artist}"
    use_track_numbers:       bool            = False
    use_album_track_numbers: bool         = False
    use_artist_subfolders:   bool           = False
    use_album_subfolders:    bool           = False
    first_artist_only:       bool            = False
    quality:                 str             = "LOSSLESS"
    allow_fallback:          bool            = True
    inter_track_delay_s:     float           = 0.5
    is_album:                bool            = False

    embed_lyrics:            bool          = False
    lyrics_providers:        list[str]     = field(
        default_factory=lambda: ["spotify", "musixmatch", "amazon", "lrclib"]
    )
    lyrics_spotify_token:    str           = ""

    enrich_metadata:    bool           = False
    enrich_providers:   list[str]      = field(
        default_factory=lambda: ["deezer", "apple", "qobuz", "tidal"]
    )
    qobuz_token:        str | None     = None


def _build_provider(name: str, opts: DownloadOptions) -> BaseProvider | None:
    from .providers.tidal import TidalProvider
    from .providers.qobuz import QobuzProvider

    if name == "tidal":
        return TidalProvider()
    if name == "qobuz":
        return QobuzProvider(qobuz_token=opts.qobuz_token)

    adapters = {
        "amazon":  ("providers.amazon",  "AmazonProvider"),
        "deezer":  ("providers.deezer",  "DeezerProvider"),
        "youtube": ("providers.youtube", "YouTubeProvider"),
        "spoti":  ("providers.spotidownloader", "SpotiDownloaderProvider"),
    }
    if name not in adapters:
        logger.warning("Unknown provider: %s", name)
        return None

    module_path, class_name = adapters[name]
    try:
        import importlib
        pkg = __name__.rsplit(".", 1)[0]
        mod = importlib.import_module(f".{module_path}", package=pkg)
        return getattr(mod, class_name)()
    except Exception as exc:
        logger.warning("Provider %s unavailable: %s", name, exc)
        return None


def download_one(
        metadata:   TrackMetadata,
        output_dir: str,
        providers:  list[BaseProvider],
        opts:       DownloadOptions,
        position:   int = 1,
) -> DownloadResult:
    errors: dict[str, str] = {}
    manager = DownloadManager()

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
            quality                 = opts.quality,          # FIX #1: quality ora propagata
            embed_lyrics            = opts.embed_lyrics,
            lyrics_providers        = opts.lyrics_providers,
            lyrics_spotify_token    = opts.lyrics_spotify_token,
            enrich_metadata         = opts.enrich_metadata,
            enrich_providers        = opts.enrich_providers,
            is_album                = opts.is_album
        )

        if result.success:
            logger.info("[%s] ✓ %s — %s", provider.name, metadata.artists, metadata.title)
            return result

        errors[provider.name] = result.error or "unknown error"
        logger.warning("[%s] ✗ %s", provider.name, result.error)

    summary = "; ".join(f"{k}: {v}" for k, v in errors.items())
    return DownloadResult.fail("none", f"All providers failed — {summary}")


class DownloadWorker:
    def __init__(
            self,
            tracks:   list[TrackMetadata],
            opts:     DownloadOptions,
            collection_name: str = "",
            is_album:     bool = False,
            is_playlist:  bool = False,
    ) -> None:
        self._tracks          = tracks
        self._opts            = opts
        self._collection_name = collection_name
        self._is_album        = is_album
        self._is_playlist     = is_playlist
        self._failed:  list[tuple[str, str, str]] = []
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
        total     = len(self._tracks)
        start     = time.perf_counter()
        base_out  = self._resolve_output_dir()

        for i, track in enumerate(self._tracks):
            self._opts.is_album = self._is_album
            position = i + 1
            print_track_header(position, total, track.title, track.artists, track.album)

            manager.start_download(track.id)

            out_dir = self._track_output_dir(base_out, track)
            result  = download_one(
                track, out_dir, self._providers, self._opts, position
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
                self._failed.append((track.title, track.artists, err))
                logger.error("[worker] Failed: %s — %s: %s", track.title, track.artists, err)
                manager.fail_download(track.id, err)

            if i < total - 1:
                time.sleep(self._opts.inter_track_delay_s)

        elapsed = time.perf_counter() - start
        self._print_summary(elapsed)
        return self._failed

    def _resolve_output_dir(self) -> str:
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
        print_summary(len(self._tracks), succeeded, self._failed, elapsed)


class SpotiflacDownloader:
    def __init__(self, opts: DownloadOptions) -> None:
        self._opts   = opts
        self._client = SpotifyMetadataClient()

    def run(self, spotify_url: str, loop_minutes: int | None = None) -> None:
        while True:
            self._run_once(spotify_url)
            if not loop_minutes or loop_minutes <= 0:
                break
            print(f"\nNext run in {loop_minutes} minutes…")
            time.sleep(loop_minutes * 60)

    def _run_once(self, spotify_url: str) -> None:
        print("Fetching metadata…")
        try:
            collection_name, tracks = self._client.get_url(spotify_url)
        except SpotiflacError as exc:
            logger.error("Metadata fetch failed: %s", exc)
            print(f"Error: {exc}")
            return

        if not tracks:
            print("No tracks found.")
            return

        missing_isrc = [t for t in tracks if not t.isrc]
        if missing_isrc:
            print(f"Resolving ISRC for {len(missing_isrc)} track(s)…")
            try:
                from .core.isrc_helper import IsrcHelper
                from .core.http import HttpClient
                resolver = IsrcHelper(HttpClient("isrc"))
                for i, track in enumerate(tracks):
                    if not track.isrc:
                        resolved = resolver.get_isrc(track.id)
                        if resolved:
                            tracks[i] = track.model_copy(update={"isrc": resolved})
                            logger.debug("[isrc] resolved %s → %s", track.id, resolved)
            except Exception as exc:
                logger.warning("[isrc] bulk resolution failed: %s", exc)

        print(f"Found {len(tracks)} track(s) in: {collection_name}")

        from .providers.spotify_metadata import parse_spotify_url
        info        = parse_spotify_url(spotify_url)
        is_album    = info["type"] == "album"
        is_playlist = info["type"] == "playlist"
        self._opts.is_album = is_album

        manager = DownloadManager()
        for t in tracks:
            manager.add_to_queue(t.id, t.title, t.artists, t.album, t.id)

        worker = DownloadWorker(
            tracks          = tracks,
            opts            = self._opts,
            collection_name = collection_name,
            is_album        = is_album,
            is_playlist     = is_playlist,
        )
        worker.run()


def _format_seconds(seconds: float) -> str:
    s = int(round(seconds))
    parts = []
    for unit, div in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        val, s = divmod(s, div)
        if val:
            parts.append(f"{val}{unit}")
    return " ".join(parts) or "0s"
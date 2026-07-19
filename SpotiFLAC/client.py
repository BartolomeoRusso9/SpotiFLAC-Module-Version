"""
SpotiFLAC/client.py
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Optional, Type

from .core.http import NetworkManager
from .core.models import TrackMetadata
from .downloader import DownloadOptions, SpotiflacDownloader
from .providers.spotify_metadata import SpotifyMetadataClient, parse_spotify_url

logger = logging.getLogger("SpotiFLAC")


def _setup_logger(level: int) -> logging.Logger:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


class AsyncSpotiFLAC:
    """
    Client asincrono nativo per SpotiFLAC.

    Uso consigliato (garantisce chiusura pulita delle risorse HTTP):

        async with AsyncSpotiFLAC(output_dir="./downloads") as client:
            await client.download_track("https://open.spotify.com/track/...")
            playlist_meta, tracks = await client.get_playlist("https://...")

    Se usato senza context manager, ricordarsi di chiamare `await client.aclose()`.
    """

    def __init__(
        self,
        output_dir: str,
        services: list[str] | None = None,
        filename_format: str = "{title} - {artist}",
        use_track_numbers: bool = False,
        use_album_track_numbers: bool = False,
        use_artist_subfolders: bool = False,
        use_album_subfolders: bool = False,
        allow_fallback: bool = True,
        quality: str = "LOSSLESS",
        first_artist_only: bool = False,
        include_featuring: bool = False,
        log_level: int = logging.WARNING,
        output_path: str | None = None,
        embed_lyrics: bool = True,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = True,
        enrich_providers: list[str] | None = None,
        qobuz_token: str | None = None,
        qobuz_local_api_url: str | None = None,
        track_max_retries: int = 0,
        post_download_action: str = "none",
        post_download_command: str = "",
        tidal_custom_api: str | None = None,
        timeout_s: int | None = None,
        max_concurrent_downloads: int = 2,
        sync_extensions: bool = True,
        use_extensions_fallback: bool = True,
    ) -> None:
        self._logger = _setup_logger(log_level)
        self._sync_extensions_on_enter = sync_extensions
        self._entered = False

        self._opts = DownloadOptions(
            output_dir=output_dir,
            services=services or ["tidal"],
            filename_format=filename_format,
            use_track_numbers=use_track_numbers,
            use_album_track_numbers=use_album_track_numbers,
            use_artist_subfolders=use_artist_subfolders,
            use_album_subfolders=use_album_subfolders,
            allow_fallback=allow_fallback,
            quality=quality,
            first_artist_only=first_artist_only,
            include_featuring=include_featuring,
            output_path=output_path,
            embed_lyrics=embed_lyrics,
            lyrics_providers=lyrics_providers
            or ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
            enrich_metadata=enrich_metadata,
            enrich_providers=enrich_providers
            or ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
            qobuz_token=qobuz_token,
            qobuz_local_api_url=qobuz_local_api_url,
            track_max_retries=track_max_retries,
            post_download_action=post_download_action,
            post_download_command=post_download_command,
            tidal_custom_api=tidal_custom_api,
            timeout_s=timeout_s,
            max_concurrent_downloads=max_concurrent_downloads,
            auto_pair_extensions=use_extensions_fallback,
        )

        self._downloader = SpotiflacDownloader(self._opts)
        self._metadata_client: SpotifyMetadataClient | None = None

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AsyncSpotiFLAC":
        await NetworkManager.get_async_client_safe()

        if self._sync_extensions_on_enter:
            try:
                from .extensions.manager import ExtensionManager

                await asyncio.to_thread(ExtensionManager, auto_install_downloads=True)
            except Exception as exc:
                self._logger.warning("[client] Extension sync skipped: %s", exc)

        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await NetworkManager.aclose_loop_client()
        self._entered = False

    # ------------------------------------------------------------------
    # API asincrona pubblica
    # ------------------------------------------------------------------

    async def download_track(
        self, url: str, *, loop_minutes: int | None = None
    ) -> list[TrackMetadata]:
        self._ensure_entered()
        return await self._downloader._run_once_async(url)

    async def download_batch(
        self, urls: list[str], *, loop_minutes: int | None = None
    ) -> None:
        self._ensure_entered()
        await self._downloader.run_async(urls, loop_minutes=loop_minutes)

    async def get_playlist(self, url: str) -> tuple[dict, list[TrackMetadata]]:
        self._ensure_entered()
        collection_name, tracks, info = await self._downloader._resolve_metadata_async(
            url
        )
        return {"name": collection_name, **info}, tracks

    async def get_track_metadata(self, url_or_id: str) -> TrackMetadata:
        self._ensure_entered()
        client = self._get_metadata_client()
        if url_or_id.startswith("http") or url_or_id.startswith("spotify:"):
            info = parse_spotify_url(url_or_id)
            return await client.get_track_async(info["id"])
        return await client.get_track_async(url_or_id)

    async def search(self, query: str, limit: int = 20) -> dict[str, list]:
        self._ensure_entered()
        client = self._get_metadata_client()
        return await client.search_async(query, limit=limit)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_metadata_client(self) -> SpotifyMetadataClient:
        if self._metadata_client is None:
            self._metadata_client = SpotifyMetadataClient()
        return self._metadata_client

    def _ensure_entered(self) -> None:
        if not self._entered:
            self._logger.debug(
                "[client] AsyncSpotiFLAC used outside of 'async with' — "
                "resources will not be closed automatically."
            )


# ---------------------------------------------------------------------------
# Wrapper sincrono retrocompatibile
# ---------------------------------------------------------------------------


def SpotiFLAC(
    url: str | list[str],
    output_dir: str,
    services: list[str] | None = None,
    filename_format: str = "{title} - {artist}",
    use_track_numbers: bool = False,
    use_album_track_numbers: bool = False,
    use_artist_subfolders: bool = False,
    use_album_subfolders: bool = False,
    loop: int | None = None,
    allow_fallback: bool = True,
    quality: str = "LOSSLESS",
    first_artist_only: bool = False,
    include_featuring: bool = False,
    log_level: int = logging.WARNING,
    output_path: str | None = None,
    embed_lyrics: bool = True,
    lyrics_providers: list[str] | None = None,
    enrich_metadata: bool = True,
    enrich_providers: list[str] | None = None,
    qobuz_token: str | None = None,
    qobuz_local_api_url: str | None = None,
    track_max_retries: int = 0,
    post_download_action: str = "none",
    post_download_command: str = "",
    tidal_custom_api: str | None = None,
    timeout_s: int | None = None,
    max_concurrent_downloads: int = 2,
    sync_extensions: bool = True,
    use_extensions_fallback: bool = True,
) -> None:
    """
    Wrapper SINCRONO retrocompatibile.

    Firma e comportamento osservabile identici alla vecchia `SpotiFLAC()`:
    chi la chiama da codice sincrono non deve cambiare nulla. Internamente
    istanzia `AsyncSpotiFLAC` e la esegue con `asyncio.run()`, garantendo un
    unico event loop pulito per l'esecuzione.
    """

    async def _run() -> None:
        async with AsyncSpotiFLAC(
            output_dir=output_dir,
            services=services,
            filename_format=filename_format,
            use_track_numbers=use_track_numbers,
            use_album_track_numbers=use_album_track_numbers,
            use_artist_subfolders=use_artist_subfolders,
            use_album_subfolders=use_album_subfolders,
            allow_fallback=allow_fallback,
            quality=quality,
            first_artist_only=first_artist_only,
            include_featuring=include_featuring,
            log_level=log_level,
            output_path=output_path,
            embed_lyrics=embed_lyrics,
            lyrics_providers=lyrics_providers,
            enrich_metadata=enrich_metadata,
            enrich_providers=enrich_providers,
            qobuz_token=qobuz_token,
            qobuz_local_api_url=qobuz_local_api_url,
            track_max_retries=track_max_retries,
            post_download_action=post_download_action,
            post_download_command=post_download_command,
            tidal_custom_api=tidal_custom_api,
            timeout_s=timeout_s,
            max_concurrent_downloads=max_concurrent_downloads,
            sync_extensions=sync_extensions,
            use_extensions_fallback=use_extensions_fallback,
        ) as client:
            await client.download_batch(
                [url] if isinstance(url, str) else list(url),
                loop_minutes=loop,
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n\n[!] Operation interrupted by user.")
    except Exception as e:
        logger.error("Critical error during execution: %s", e)


__all__ = ["AsyncSpotiFLAC", "SpotiFLAC"]

"""
SpotiFLAC/client.py
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, Optional, Type

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
        )

        self._downloader = SpotiflacDownloader(self._opts)
        self._metadata_client: SpotifyMetadataClient | None = None

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AsyncSpotiFLAC":
        # Inizializza (lazy, ma esplicito qui) l'httpx.AsyncClient condiviso
        # per l'event loop corrente. NetworkManager fa già connection
        # pooling e dedup per-loop; chiamarlo qui dentro __aenter__ assicura
        # che il client sia pronto PRIMA che qualunque provider lo usi,
        # evitando la creazione lazy sparsa nel primo metodo chiamato.
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
        """Chiude il client HTTP condiviso legato all'event loop corrente."""
        await NetworkManager.aclose_loop_client()
        self._entered = False

    # ------------------------------------------------------------------
    # API asincrona pubblica
    # ------------------------------------------------------------------

    async def download_track(
        self, url: str, *, loop_minutes: int | None = None
    ) -> list[TrackMetadata]:
        """
        Scarica una singola URL (track/album/playlist/artista/URL nativo di
        un altro provider). Restituisce le eventuali tracce fallite
        (per un'eventuale retry manuale da parte del chiamante).
        """
        self._ensure_entered()
        return await self._downloader._run_once_async(url)

    async def download_batch(
        self, urls: list[str], *, loop_minutes: int | None = None
    ) -> None:
        """Scarica una lista di URL in sequenza, con loop di retry opzionale."""
        self._ensure_entered()
        await self._downloader.run_async(urls, loop_minutes=loop_minutes)

    async def get_playlist(self, url: str) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera solo i metadati di una playlist/album/artista, senza
        scaricare nulla. Utile per costruire UI o code di lavoro custom.
        """
        self._ensure_entered()
        collection_name, tracks, info = await self._downloader._resolve_metadata_async(
            url
        )
        return {"name": collection_name, **info}, tracks

    async def get_track_metadata(self, url_or_id: str) -> TrackMetadata:
        """Recupera i metadati di una singola track Spotify."""
        self._ensure_entered()
        client = self._get_metadata_client()
        if url_or_id.startswith("http") or url_or_id.startswith("spotify:"):
            info = parse_spotify_url(url_or_id)
            return await client.get_track_async(info["id"])
        return await client.get_track_async(url_or_id)

    async def search(self, query: str, limit: int = 20) -> dict[str, list]:
        """Ricerca unificata (tracks/albums/artists/playlists) su Spotify."""
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
            # Non è un errore bloccante: NetworkManager crea comunque il
            # client lazy alla prima richiesta. Segnaliamo solo che l'uso
            # raccomandato è tramite `async with AsyncSpotiFLAC(...) as c:`
            # per garantire cleanup deterministico delle connessioni.
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
) -> None:
    """
    Wrapper SINCRONO retrocompatibile.

    Firma e comportamento osservabile identici alla vecchia `SpotiFLAC()`:
    chi la chiama da codice sincrono (script, notebook, bot Telegram non
    async, ecc.) non deve cambiare nulla. Internamente istanzia
    `AsyncSpotiFLAC` e la esegue con `asyncio.run()`, quindi apre/chiude
    UN SOLO event loop per l'intera chiamata — niente più
    "un event loop per thread".

    Se sei già dentro un contesto async, usa direttamente `AsyncSpotiFLAC`
    con `async with`; chiamare questo wrapper da dentro un event loop già
    attivo (es. Jupyter, un altro `async def`) solleverebbe
    "asyncio.run() cannot be called from a running event loop" — in quel
    caso, semplicemente `await`a `AsyncSpotiFLAC` direttamente.
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
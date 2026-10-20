from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast

from mornye.core.config import DownloadRequest
from mornye.core.models import DownloadResult, TrackMetadata
from mornye.application.provider_executor import ProviderExecutor

if TYPE_CHECKING:
    from mornye.downloader import DownloadOptions


class LegacyDownloadAdapter(ProviderExecutor):
    """Compatibility adapter that keeps the legacy downloader behind the
    application-layer execution contract.

    The app layer uses the provider resolver to pick a candidate, then hands the
    final provider + source pair to this adapter. The adapter bridges back into
    the legacy downloader implementation without exposing its orchestration
    details to the rest of the codebase.
    """

    def __init__(
        self,
        downloader: object,
        prefetched: dict[str, TrackMetadata] | None = None,
        options: object | None = None,
        forward_metadata: bool = False,
    ) -> None:
        super().__init__()
        self._downloader = downloader
        self._prefetched = prefetched or {}
        self._options = options
        self._last_result: DownloadResult | None = None
        self._forward_metadata = forward_metadata

    @property
    def downloader(self) -> Any:
        """Return the wrapped engine for compatibility lifecycle wiring."""
        return self._downloader

    @property
    def last_result(self) -> DownloadResult | None:
        """Return the latest structured result exposed by the legacy engine."""
        return self._last_result

    @property
    def forwards_metadata(self) -> bool:
        return self._forward_metadata

    def set_prefetched(self, metadata: dict[str, TrackMetadata]) -> None:
        self._prefetched = dict(metadata)

    def set_output_dir(self, output_dir: str) -> None:
        """Keep both the adapter options and wrapped legacy engine in sync."""
        for options in (
            self._options,
            getattr(self._downloader, "_opts", None),
        ):
            if options is not None and hasattr(options, "output_dir"):
                setattr(options, "output_dir", output_dir)

    async def resolve_metadata(self, source: str) -> object:
        """Compatibility bridge for callers still using legacy metadata APIs."""
        resolver = getattr(self._downloader, "_resolve_metadata_async", None)
        if not callable(resolver):
            raise AttributeError(
                "legacy downloader does not implement _resolve_metadata_async()"
            )
        return await resolver(source)

    async def run_async(self, source: str | list[str], **kwargs: Any) -> Any:
        runner = getattr(self._downloader, "run_async")
        return await runner(source, **kwargs)

    async def run_csv_async(self, path: str, **kwargs: Any) -> Any:
        runner = getattr(self._downloader, "run_csv_async")
        return await runner(path, **kwargs)

    async def run_playlists_async(self, urls: list[str], **kwargs: Any) -> Any:
        runner = getattr(self._downloader, "run_playlists_async")
        return await runner(urls, **kwargs)

    @staticmethod
    def options_for(request: DownloadRequest) -> object:
        """Translate request configuration into legacy downloader options."""
        from mornye.downloader import DownloadOptions

        config = request.config
        return DownloadOptions(
            output_dir=str(config.output.directory),
            services=list(config.download.services),
            filename_format=config.output.filename_format,
            use_artist_subfolders=config.output.artist_subfolders,
            use_album_subfolders=config.output.album_subfolders,
            create_playlist_subfolders=config.output.playlist_subfolders,
            quality=config.download.quality,
            allow_fallback=config.download.allow_fallback,
            max_concurrent_downloads=config.download.max_concurrent,
            track_max_retries=config.download.retries,
            timeout_s=config.download.timeout,
            resume=config.download.resume,
            embed_lyrics=config.lyrics.enabled,
            lyrics_providers=config.lyrics.providers,
            save_lrc=config.lyrics.save_lrc,
            apple_lyrics_word_by_word=config.lyrics.word_by_word,
            enrich_metadata=config.metadata.enrich,
            enrich_providers=config.metadata.providers,
            output_path=None,
        )

    @classmethod
    def from_options(
        cls,
        options: object,
        prefetched: dict[str, TrackMetadata] | None = None,
        forward_metadata: bool = True,
    ) -> "LegacyDownloadAdapter":
        from mornye.downloader import MornyeDownloader

        return cls(
            MornyeDownloader(cast("DownloadOptions", options)),
            prefetched=prefetched,
            options=options,
            forward_metadata=forward_metadata,
        )

    def _service_name(self, provider: str) -> str:
        """Map an application provider id to the legacy service spelling."""
        configured = list(getattr(self._options, "services", []) or [])
        normalized = (
            provider.removeprefix("ext:").removesuffix("-web").removesuffix("-py")
        )
        for service in configured:
            service_id = (
                str(service)
                .removeprefix("ext:")
                .removesuffix("-web")
                .removesuffix("-py")
            )
            if service_id == normalized:
                return str(service)
        return provider if provider.startswith("ext:") else f"ext:{provider}-web"

    def _provider_downloader(self, provider: str) -> object:
        """Create a legacy engine constrained to one application provider."""
        if self._options is None:
            return self._downloader

        from mornye.downloader import MornyeDownloader

        scoped_options = cast("DownloadOptions", copy.copy(self._options))
        scoped_options.services = [self._service_name(provider)]
        return MornyeDownloader(scoped_options)

    async def execute(self, provider: str, source: str) -> None:
        self._last_result = None
        downloader = self._provider_downloader(provider)
        batch_runner = getattr(downloader, "run_tracks_async", None)
        if self._prefetched and callable(batch_runner):
            result = await batch_runner([source], prefetched=self._prefetched)
            if isinstance(result, DownloadResult):
                self._last_result = result
            else:
                self._last_result = self._result_from_engine(
                    downloader, provider, source
                )
            return
        runner = getattr(downloader, "run_async", None)
        if runner is None:
            raise AttributeError("legacy downloader does not implement run_async()")
        if callable(runner):
            result = runner(source)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, DownloadResult):
                self._last_result = result
            else:
                self._last_result = self._result_from_engine(
                    downloader, provider, source
                )

    def _result_from_engine(
        self, downloader: object, provider: str, source: str
    ) -> DownloadResult:
        results = getattr(downloader, "_last_results", {})
        if isinstance(results, dict):
            for result in results.values():
                if isinstance(result, DownloadResult):
                    return result.model_copy(
                        update={"provider": provider, "source": source}
                    )
        raise RuntimeError("legacy downloader returned no structured result")

    async def __call__(self, provider: str, source: str) -> None:
        await self.execute(provider, source)

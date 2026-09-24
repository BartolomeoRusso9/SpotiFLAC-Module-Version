from __future__ import annotations

import copy
from typing import TYPE_CHECKING, cast

from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.application.provider_executor import ProviderExecutor

if TYPE_CHECKING:
    from SpotiFLAC.downloader import DownloadOptions


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
    def downloader(self) -> object:
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

    async def resolve_metadata(self, source: str) -> object:
        """Resolve metadata with the wrapped engine's real catalogue client."""
        resolver = getattr(self._downloader, "_resolve_metadata_async", None)
        if not callable(resolver):
            raise AttributeError(
                "legacy downloader does not implement _resolve_metadata_async()"
            )
        return await resolver(source)

    @staticmethod
    def options_for(request: DownloadRequest) -> object:
        from SpotiFLAC.downloader import DownloadOptions

        config = request.config
        return DownloadOptions(
            output_dir=str(config.output.directory),
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
            output_path=str(config.output.directory),
        )

    @classmethod
    def from_options(
        cls,
        options: object,
        prefetched: dict[str, TrackMetadata] | None = None,
        forward_metadata: bool = True,
    ) -> "LegacyDownloadAdapter":
        from SpotiFLAC.downloader import SpotiflacDownloader

        return cls(
            SpotiflacDownloader(cast("DownloadOptions", options)),
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

        from SpotiFLAC.downloader import SpotiflacDownloader

        scoped_options = cast("DownloadOptions", copy.copy(self._options))
        scoped_options.services = [self._service_name(provider)]
        return SpotiflacDownloader(scoped_options)

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

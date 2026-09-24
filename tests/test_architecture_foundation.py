from SpotiFLAC.extensions.conformance import validate_extension
import asyncio
import ast
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from SpotiFLAC import (
    DownloadFailure,
    DownloadReport,
    DownloadRequest,
    SpotiFLACConfig,
)
from SpotiFLAC.application import (
    ApiAdapter,
    DownloadService,
    EventBus,
    ExtensionService,
    JobService,
    LegacyDownloadAdapter,
    MetadataService,
    OutputService,
    ApplicationDownloadWorker,
    ProviderExecutor,
    ProviderResolver,
    QueueService,
    PostProcessingService,
)
from SpotiFLAC.application.pipeline import (
    DownloadContext,
    DownloadPipeline,
    CanvasStep,
    LibraryIndexStep,
    LyricsStep,
    TranscodeStep,
    ProviderStep,
    ResolveStep,
    TagStep,
    ValidateStep,
)
from SpotiFLAC.core.repositories import ExtensionRepository, JobRepository
from SpotiFLAC.core.providers import (
    ExtensionManifest,
    ProviderCandidate,
    ProviderProfile,
)
from SpotiFLAC.client import AsyncSpotiFLAC
from SpotiFLAC.launcher import build_cli_download_service, run_download_from_cfg
from tests.fake_providers import FakeProvider
from SpotiFLAC.webapi import ApiDeps, build_v1_router

from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.core.errors import (
    ErrorKind,
    NetworkError,
    SpotiflacError,
    TrackNotFoundError,
)
from SpotiFLAC.core.retry import RetryPolicy
from SpotiFLAC.downloader import DownloadOptions


def test_spotiflac_config_can_be_built_from_legacy_options():
    cfg = SpotiFLACConfig.from_legacy_options(
        DownloadOptions(
            output_dir="./downloads",
            quality="HI_RES_LOSSLESS",
            allow_fallback=False,
            max_concurrent_downloads=4,
        )
    )

    assert cfg.output.directory == Path("./downloads")
    assert cfg.download.quality == "HI_RES_LOSSLESS"
    assert cfg.download.allow_fallback is False
    assert cfg.download.max_concurrent == 4


def test_application_imports_legacy_downloader_only_through_adapter():
    application_dir = Path(__file__).parents[1] / "SpotiFLAC" / "application"

    for path in application_dir.glob("*.py"):
        if path.name in {"legacy_download_adapter.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert "SpotiFLAC.downloader" not in imports, path.name


def test_tui_does_not_import_or_construct_legacy_downloader():
    tui_dir = Path(__file__).parents[1] / "SpotiFLAC" / "tui"

    for path in tui_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert "SpotiFLAC.downloader" not in imports, path.name
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SpotiflacDownloader"
            for node in ast.walk(tree)
        ), path.name


def test_application_core_does_not_import_ui_or_web_implementation():
    application_dir = Path(__file__).parents[1] / "SpotiFLAC" / "application"
    forbidden = {
        "SpotiFLAC.app",
        "SpotiFLAC.tui",
        "SpotiFLAC.webapp",
        "SpotiFLAC.webapi",
    }

    for path in application_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not imports & forbidden, path.name


def test_post_processing_is_exposed_as_application_boundary():
    assert PostProcessingService.__module__ == "SpotiFLAC.application.post_processing"
    assert (
        "PostProcessingService"
        in (
            Path(__file__).parents[1] / "SpotiFLAC" / "application" / "__init__.py"
        ).read_text()
    )


def test_download_worker_orchestration_is_application_owned():
    assert (
        ApplicationDownloadWorker.__module__ == "SpotiFLAC.application.download_worker"
    )
    assert (
        "class LegacyDownloadWorker"
        in (Path(__file__).parents[1] / "SpotiFLAC" / "downloader.py").read_text()
    )


def test_output_service_creates_configured_library_paths(tmp_path):
    config = SpotiFLACConfig()
    config.output.directory = tmp_path
    config.output.artist_subfolders = True
    config.output.album_subfolders = True
    service = OutputService(config.output)
    track = TrackMetadata(
        id="track-1",
        title="Song / Live",
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )

    path = service.path_for(track, collection_name="Playlist: 2026", is_playlist=True)

    assert path.parent == tmp_path / "Playlist_ 2026" / "Artist" / "Album"
    assert path.name == "Song Live - Artist.flac"
    assert path.parent.is_dir()


def test_download_request_and_report_contract():
    cfg = SpotiFLACConfig()
    request = DownloadRequest(sources=["spotify:track:abc123"], config=cfg)

    track = TrackMetadata(
        id="abc123",
        title="Example",
        artists="Artist A",
        album="Album",
        album_artist="Artist A",
    )
    started_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    finished_at = started_at + timedelta(minutes=2)

    report = DownloadReport(
        succeeded=[DownloadResult.ok("tidal", "/tmp/example.flac")],
        failed=[
            DownloadFailure(
                track=track,
                reason="timeout",
                provider="tidal",
                attempts=1,
                retryable=True,
            )
        ],
        skipped=[],
        started_at=started_at,
        finished_at=finished_at,
    )

    assert request.sources == ["spotify:track:abc123"]
    assert report.total == 2
    assert report.success_count == 1
    assert report.failed[0].reason == "timeout"


def test_download_service_builds_structured_report_from_request():
    bus = EventBus()
    events = []
    bus.subscribe("download.started", lambda payload: events.append(payload))

    service = DownloadService(event_bus=bus)
    request = DownloadRequest(
        sources=["spotify:track:abc123", "spotify:track:missing"],
        config=SpotiFLACConfig(),
    )

    report = asyncio.run(service.download(request))

    assert report.total == 2
    assert report.success_count == 1
    assert report.failed[0].reason == "source_not_supported"
    assert report.failed[0].provider == "tidal"
    assert events and events[0]["source"] == "spotify:track:abc123"


def test_download_service_builds_legacy_download_options_from_config():
    service = DownloadService()
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    opts = service.legacy_options_for(request)

    assert opts.output_dir == str(request.config.output.directory)
    assert opts.quality == request.config.download.quality
    assert opts.max_concurrent_downloads == request.config.download.max_concurrent


def test_download_service_uses_legacy_downloader_for_sources(monkeypatch):
    calls = {}

    async def fake_run_async(self, input_url, loop_minutes=None):
        calls["url"] = input_url
        calls["quality"] = self._opts.quality
        calls["output_dir"] = self._opts.output_dir
        return DownloadResult.ok("tidal", "/music/abc123.flac", source=input_url)

    monkeypatch.setattr(
        "SpotiFLAC.downloader.SpotiflacDownloader.run_async",
        fake_run_async,
    )

    service = DownloadService()
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    report = asyncio.run(service.download(request))

    assert calls["url"] == "spotify:track:abc123"
    assert calls["quality"] == "LOSSLESS"
    assert calls["output_dir"] == str(request.config.output.directory)
    assert report.success_count == 1


def test_v1_download_route_uses_application_service_boundary():
    seen = {}

    class FakeDownloadService:
        async def download(self, request):
            seen["sources"] = list(request.sources)
            seen["quality"] = request.config.download.quality
            seen["output_dir"] = str(request.config.output.directory)
            return DownloadReport(
                succeeded=[DownloadResult.ok("tidal", "/tmp/example.flac")],
                failed=[],
                skipped=[],
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

    app = FastAPI()
    app.include_router(
        build_v1_router(
            ApiDeps(
                api_for=lambda _request: SimpleNamespace(download_dir="/downloads"),
                download_service=FakeDownloadService(),
                job_queue=None,
            )
        )
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/downloads",
        json={"url": "https://open.spotify.com/track/x", "output_dir": "/music"},
    )

    assert response.status_code == 202
    assert seen["sources"] == ["https://open.spotify.com/track/x"]
    assert seen["quality"] == "LOSSLESS"
    assert seen["output_dir"] == "/music"
    assert response.json()["id"] == "direct"


def test_download_service_runs_injected_post_processors(monkeypatch):
    calls = []

    async def fake_run_async(self, input_url, loop_minutes=None):
        return DownloadResult.ok("tidal", "/music/abc123.flac", source=input_url)

    async def fake_tagger(path, metadata):
        calls.append(("tag", path, metadata.title))

    monkeypatch.setattr(
        "SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run_async
    )
    track = TrackMetadata(
        id="track-1",
        title="Song",
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
        prefetched={"spotify:track:abc123": track},
    )

    report = asyncio.run(DownloadService(tagger=fake_tagger).download(request))

    assert report.success_count == 1
    assert calls == [("tag", "/music/abc123.flac", "Song")]


def test_download_service_resolves_metadata_for_post_processors(monkeypatch):
    calls = []

    async def fake_run_async(self, input_url, loop_minutes=None):
        return DownloadResult.ok("tidal", "/music/abc123.flac", source=input_url)

    async def fake_tagger(path, metadata):
        calls.append(metadata.title)

    monkeypatch.setattr(
        "SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run_async
    )
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    asyncio.run(DownloadService(tagger=fake_tagger).download(request))

    assert calls == ["Example Track"]


def test_download_service_applies_retry_policy(monkeypatch):
    attempts = 0

    async def flaky_run(self, input_url, loop_minutes=None):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary")
        return DownloadResult.ok("tidal", "/music/abc123.flac", source=input_url)

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader.run_async", flaky_run)
    config = SpotiFLACConfig()
    config.download.retries = 2
    request = DownloadRequest(sources=["spotify:track:abc123"], config=config)

    report = asyncio.run(DownloadService().download(request))

    assert attempts == 3
    assert report.success_count == 1


def test_async_client_exposes_application_download_entrypoint(monkeypatch):
    class FakeDownloadService:
        async def download(self, request):
            return DownloadReport(
                succeeded=[DownloadResult.ok("tidal", "/tmp/track.flac")],
                failed=[],
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

    client = AsyncSpotiFLAC(output_dir="./downloads", sync_extensions=False)
    client._download_service = FakeDownloadService()
    request = DownloadRequest(
        sources=["spotify:track:abc123"], config=SpotiFLACConfig()
    )

    report = asyncio.run(client.download_request(request))

    assert report.success_count == 1
    assert client.application_config().output.directory == Path("downloads")


def test_download_service_can_reuse_configured_legacy_downloader(monkeypatch):
    calls = []

    class FakeDownloader:
        async def run_async(self, source):
            calls.append(source)
            return DownloadResult.ok("legacy", "/music/configured.flac", source=source)

    request = DownloadRequest(
        sources=["spotify:track:configured"], config=SpotiFLACConfig()
    )
    report = asyncio.run(DownloadService(downloader=FakeDownloader()).download(request))

    assert calls == ["spotify:track:configured"]
    assert report.success_count == 1


def test_download_service_accepts_extension_aware_provider_resolver(monkeypatch):
    selected = []

    async def fake_run(self, source):
        selected.append(source)
        return DownloadResult.ok(
            "extension-provider", "/music/extension.flac", source=source
        )

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run)
    resolver = ProviderResolver([ProviderProfile("extension-provider", priority=10)])
    request = DownloadRequest(
        sources=["spotify:track:extension"],
        config=SpotiFLACConfig(),
    )

    report = asyncio.run(DownloadService(provider_resolver=resolver).download(request))

    assert selected == ["spotify:track:extension"]
    assert report.succeeded[0].provider == "extension-provider"


def test_async_client_uses_application_service_with_legacy_adapter():
    client = AsyncSpotiFLAC(output_dir="./downloads", sync_extensions=False)

    assert isinstance(
        client._download_service._provider_executor, LegacyDownloadAdapter
    )
    assert client._download_service._downloader is client._downloader
    assert client._download_service._provider_executor.downloader is client._downloader


def test_async_client_builds_service_via_legacy_factory(monkeypatch):
    seen = {}
    original = DownloadService.from_legacy_options

    def fake_from_legacy_options(
        cls, options, *, event_bus=None, prefetched=None, downloader=None
    ):
        seen["options"] = options
        seen["prefetched"] = prefetched
        seen["downloader"] = downloader
        return original(
            options, event_bus=event_bus, prefetched=prefetched, downloader=downloader
        )

    monkeypatch.setattr(
        DownloadService, "from_legacy_options", classmethod(fake_from_legacy_options)
    )

    client = AsyncSpotiFLAC(output_dir="./downloads", sync_extensions=False)

    assert seen["options"] is client._opts
    assert seen["downloader"] is client._downloader
    assert isinstance(
        client._download_service._provider_executor, LegacyDownloadAdapter
    )


def test_async_client_batch_and_track_entrypoints_use_application_service(monkeypatch):
    client = AsyncSpotiFLAC(output_dir="./downloads", sync_extensions=False)
    seen = []

    async def fake_download(request):
        seen.append(
            (request.sources, request.config.download.quality, request.prefetched)
        )
        return DownloadReport(
            succeeded=[],
            failed=[],
            skipped=[],
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )

    async def boom(*args, **kwargs):
        raise AssertionError("legacy downloader path should not be called")

    monkeypatch.setattr(client._download_service, "download", fake_download)
    monkeypatch.setattr(client._downloader, "run_async", boom)
    monkeypatch.setattr(client._downloader, "run_tracks_async", boom)

    asyncio.run(client.download_batch(["spotify:track:abc123"]))
    asyncio.run(
        client.download_tracks(
            ["spotify:track:def456"],
            prefetched={
                "spotify:track:def456": TrackMetadata(
                    id="def456",
                    title="Song",
                    artists="Artist",
                    album="Album",
                    album_artist="Artist",
                )
            },
        )
    )

    assert len(seen) == 2
    assert seen[0][0] == ["spotify:track:abc123"]
    assert seen[1][0] == ["spotify:track:def456"]
    assert seen[1][2]["spotify:track:def456"].id == "def456"


def test_cli_download_service_keeps_legacy_downloader_beneath_application_boundary():
    calls = []

    class FakeDownloader:
        async def run_async(self, source):
            calls.append(source)
            return DownloadResult.ok("tidal", "/music/legacy.flac", source=source)

    service = build_cli_download_service(FakeDownloader())
    request = DownloadRequest(
        sources=["spotify:track:cli"],
        config=SpotiFLACConfig(),
    )

    report = asyncio.run(service.download(request))

    assert calls == ["spotify:track:cli"]
    assert report.success_count == 1
    assert report.succeeded[0].provider == "tidal"


def test_run_download_from_cfg_uses_application_service_for_guided_downloads(
    monkeypatch,
):
    seen = {}

    class FakeDownloadService:
        @classmethod
        def from_legacy_options(cls, *args, **kwargs):
            return cls()

        def __init__(self, *args, **kwargs):
            pass

        async def download(self, request):
            seen["sources"] = list(request.sources)
            seen["quality"] = request.config.download.quality
            seen["output_dir"] = str(request.config.output.directory)
            return DownloadReport(
                succeeded=[DownloadResult.ok("tidal", "/tmp/example.flac")],
                failed=[],
                skipped=[],
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

    monkeypatch.setattr("SpotiFLAC.launcher.DownloadService", FakeDownloadService)
    monkeypatch.setattr(
        "SpotiFLAC.launcher._run_download_async",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy path used")
        ),
    )

    cfg = {
        "url": "https://open.spotify.com/track/x",
        "output_dir": "/music",
        "services": ["tidal"],
        "quality": "LOSSLESS",
        "filename_format": "{title} - {artist}",
        "use_track_numbers": False,
        "use_album_track_numbers": False,
        "use_artist_subfolders": False,
        "use_album_subfolders": False,
        "create_playlist_subfolders": True,
        "first_artist_only": False,
        "include_featuring": True,
        "embed_lyrics": False,
        "lyrics_providers": ["spotify"],
        "enrich_metadata": False,
        "enrich_providers": ["tidal"],
        "track_max_retries": 0,
        "allow_fallback": True,
        "resume": True,
        "loop": None,
    }

    asyncio.run(run_download_from_cfg(cfg, logging.INFO))

    assert seen["sources"] == ["https://open.spotify.com/track/x"]
    assert seen["quality"] == "LOSSLESS"
    assert seen["output_dir"] == "/music"


def test_provider_executor_executes_candidate_chain_via_application_layer():
    attempts = []

    async def execute(provider: str, source: str) -> DownloadResult:
        attempts.append((provider, source))
        return DownloadResult.ok(provider, "/music/provider.flac", source=source)

    resolver = ProviderResolver(
        [
            ProviderProfile("first", priority=2),
            ProviderProfile("second", priority=1),
        ]
    )
    request = DownloadRequest(
        sources=["spotify:track:provider-chain"],
        config=SpotiFLACConfig(),
    )

    executor = ProviderExecutor(execute)
    report = asyncio.run(
        DownloadService(
            provider_resolver=resolver,
            provider_executor=executor,
        ).download(request)
    )

    assert attempts == [("first", "spotify:track:provider-chain")]
    assert report.success_count == 1
    assert report.succeeded[0].provider == "first"


def test_retry_policy_uses_typed_provider_error_classification():
    policy = RetryPolicy(max_attempts=3)

    assert policy.is_retryable(NetworkError("tidal", "temporary"))
    assert not policy.is_retryable(TrackNotFoundError("tidal", "track-1"))
    assert policy.is_retryable(
        SpotiflacError(ErrorKind.UNAVAILABLE, "provider unavailable", "tidal")
    )


def test_provider_executor_applies_request_timeout():
    calls = []

    async def execute(provider, source):
        calls.append((provider, source))
        raise TimeoutError("provider timeout")

    executor = ProviderExecutor(execute)
    error = asyncio.run(
        executor.execute_with_retry("tidal", "track", RetryPolicy(1), timeout_s=1)
    )

    assert isinstance(error, TimeoutError)
    assert calls == [("tidal", "track")]


def test_download_service_propagates_provider_timeout():
    async def execute(provider, source):
        raise TimeoutError("provider timeout")

    config = SpotiFLACConfig()
    config.download.timeout = 1
    request = DownloadRequest(sources=["spotify:track:timeout"], config=config)
    report = asyncio.run(
        DownloadService(
            provider_resolver=ProviderResolver([ProviderProfile("tidal")]),
            provider_executor=execute,
        ).download(request)
    )

    assert report.success_count == 0
    assert report.failed[0].reason == "download_failed"
    assert report.failed[0].source == "spotify:track:timeout"


def test_legacy_download_adapter_executes_legacy_downloader_via_application_layer():
    calls = []

    class FakeDownloader:
        async def run_async(self, source):
            calls.append(source)
            return DownloadResult.ok("legacy", "/music/legacy.flac", source=source)

    resolver = ProviderResolver([ProviderProfile("legacy", priority=5)])
    request = DownloadRequest(
        sources=["spotify:track:legacy-compat"],
        config=SpotiFLACConfig(),
    )

    report = asyncio.run(
        DownloadService(
            provider_resolver=resolver,
            provider_executor=LegacyDownloadAdapter(FakeDownloader()),
        ).download(request)
    )

    assert calls == ["spotify:track:legacy-compat"]
    assert report.success_count == 1
    assert report.succeeded[0].provider == "legacy"


def test_legacy_adapter_forwards_explicit_prefetched_metadata():
    calls = []

    class FakeDownloader:
        async def run_tracks_async(self, sources, *, prefetched=None):
            calls.append((sources, prefetched))
            return DownloadResult.ok(
                "legacy", "/music/prefetched.flac", source=sources[0]
            )

        async def run_async(self, source):
            raise AssertionError("explicit prefetched metadata must use track runner")

    track = TrackMetadata(
        id="prefetched-1",
        title="Prefetched",
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )
    request = DownloadRequest(
        sources=["spotify:track:prefetched-1"],
        config=SpotiFLACConfig(),
        prefetched={"spotify:track:prefetched-1": track},
    )

    report = asyncio.run(
        DownloadService(
            provider_resolver=ProviderResolver([ProviderProfile("legacy")]),
            downloader=FakeDownloader(),
        ).download(request)
    )

    assert report.success_count == 1
    assert calls == [(["spotify:track:prefetched-1"], request.prefetched)]


def test_legacy_adapter_keeps_normal_requests_on_legacy_runner():
    calls = []

    class FakeDownloader:
        async def run_tracks_async(self, sources, *, prefetched=None):
            raise AssertionError("normal requests must not use track runner")

        async def run_async(self, source):
            calls.append(source)
            return DownloadResult.ok("legacy", "/music/normal.flac", source=source)

    adapter = LegacyDownloadAdapter(FakeDownloader())

    asyncio.run(adapter.execute("legacy", "spotify:track:normal-compat"))

    assert calls == ["spotify:track:normal-compat"]


def test_production_legacy_adapter_rejects_unstructured_results():
    class UntypedDownloader:
        async def run_async(self, _source):
            return None

    adapter = LegacyDownloadAdapter(UntypedDownloader())

    with pytest.raises(RuntimeError, match="structured result"):
        asyncio.run(adapter.execute("legacy", "spotify:track:untyped"))


def test_legacy_adapter_has_no_synthetic_tmp_result_path():
    adapter_source = (
        Path(__file__).parents[1]
        / "SpotiFLAC"
        / "application"
        / "legacy_download_adapter.py"
    ).read_text()
    assert "/tmp" not in adapter_source
    assert "_compatibility_result" not in adapter_source


def test_legacy_adapter_scopes_structured_options_to_selected_provider(monkeypatch):
    constructed = []
    executed = []

    class FakeDownloader:
        def __init__(self, options):
            constructed.append(list(options.services))

        async def run_async(self, source):
            executed.append(source)
            return DownloadResult.ok("qobuz", "/music/scoped.flac", source=source)

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader", FakeDownloader)
    options = SimpleNamespace(services=["ext:tidal-web"])
    adapter = LegacyDownloadAdapter.from_options(options)

    asyncio.run(adapter.execute("qobuz", "spotify:track:scoped"))

    assert constructed == [["ext:tidal-web"], ["ext:qobuz-web"]]
    assert executed == ["spotify:track:scoped"]


def test_download_service_uses_structured_legacy_result():
    class FakeDownloader:
        async def run_async(self, source):
            return DownloadResult.ok("legacy", "/music/actual.flac", source=source)

    request = DownloadRequest(
        sources=["spotify:track:structured"], config=SpotiFLACConfig()
    )
    report = asyncio.run(
        DownloadService(
            provider_resolver=ProviderResolver([ProviderProfile("legacy")]),
            provider_executor=LegacyDownloadAdapter(FakeDownloader()),
        ).download(request)
    )

    assert report.succeeded[0].file_path == "/music/actual.flac"
    assert report.succeeded[0].source == "spotify:track:structured"


def test_download_service_does_not_synthesize_missing_provider_results():
    class UnstructuredExecutor(ProviderExecutor):
        async def execute(self, provider, source):
            return None

    request = DownloadRequest(
        sources=["spotify:track:unstructured"], config=SpotiFLACConfig()
    )
    report = asyncio.run(
        DownloadService(
            provider_resolver=ProviderResolver([ProviderProfile("provider")]),
            provider_executor=UnstructuredExecutor(),
        ).download(request)
    )

    assert report.success_count == 0
    assert report.failed[0].reason == "provider_returned_no_result"


def test_skipped_provider_results_preserve_source_and_report_skip():
    async def execute(provider, source):
        return DownloadResult.skipped_result(
            provider, "/music/existing.flac", source=source
        )

    source = "spotify:track:already-downloaded"
    report = asyncio.run(
        DownloadService(
            provider_resolver=ProviderResolver([ProviderProfile("tidal")]),
            provider_executor=execute,
        ).download(DownloadRequest(sources=[source], config=SpotiFLACConfig()))
    )

    assert report.succeeded == []
    assert report.skipped[0].provider == "tidal"
    assert report.total == 1


def test_application_metadata_is_forwarded_without_legacy_reresolution():
    metadata_calls = 0
    forwarded = []
    track = TrackMetadata(
        id="single-resolution",
        title="Resolved Once",
        artists="Artist",
        album="Album",
        album_artist="Artist",
        external_url="https://open.spotify.com/track/single-resolution",
    )

    class CountingMetadataService:
        async def resolve(self, _request):
            nonlocal metadata_calls
            metadata_calls += 1
            return [track]

    class FakeDownloader:
        async def run_tracks_async(self, sources, *, prefetched=None):
            forwarded.append((sources, prefetched))
            return DownloadResult.ok(
                "tidal", "/music/resolved-once.flac", source=sources[0]
            )

        async def run_async(self, _source):
            raise AssertionError("legacy metadata resolver was used")

    request = DownloadRequest(
        sources=["spotify:track:single-resolution"], config=SpotiFLACConfig()
    )
    report = asyncio.run(
        DownloadService(
            metadata_service=CountingMetadataService(),
            provider_resolver=ProviderResolver([ProviderProfile("tidal")]),
            provider_executor=LegacyDownloadAdapter(
                FakeDownloader(), forward_metadata=True
            ),
        ).download(request)
    )

    assert metadata_calls == 1
    assert len(forwarded) == 1
    assert forwarded[0][0] == [request.sources[0]]
    assert forwarded[0][1][request.sources[0]] == track
    assert forwarded[0][1][track.external_url] == track
    assert report.succeeded[0].file_path == "/music/resolved-once.flac"


def test_metadata_service_uses_injected_real_resolver():
    track = TrackMetadata(
        id="real-metadata",
        title="Resolved",
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )

    async def resolve(_source):
        return "Album", [track], {}

    request = DownloadRequest(
        sources=["https://open.spotify.com/track/real-metadata"],
        config=SpotiFLACConfig(),
    )

    resolved = asyncio.run(MetadataService(resolve).resolve(request))

    assert resolved == [track]


def test_download_service_falls_back_to_next_provider_candidate():
    attempts = []

    async def execute(provider, source):
        attempts.append(provider)
        if provider == "first":
            raise RuntimeError("provider unavailable")
        return DownloadResult.ok(provider, "/music/fallback.flac", source=source)

    resolver = ProviderResolver(
        [
            ProviderProfile("first", priority=2),
            ProviderProfile("second", priority=1),
        ]
    )
    request = DownloadRequest(
        sources=["spotify:track:fallback"], config=SpotiFLACConfig()
    )

    report = asyncio.run(
        DownloadService(
            provider_resolver=resolver,
            provider_executor=execute,
        ).download(request)
    )

    assert attempts == ["first", "second"]
    assert report.success_count == 1
    assert report.succeeded[0].provider == "second"


def test_fake_provider_framework_models_fallback_without_network():
    first = FakeProvider("first", failures=1)
    second = FakeProvider("second")

    async def execute(provider, source):
        await {"first": first, "second": second}[provider].download(source)
        return DownloadResult.ok(provider, "/music/fake.flac", source=source)

    resolver = ProviderResolver(
        [ProviderProfile("first", priority=2), ProviderProfile("second", priority=1)]
    )
    request = DownloadRequest(sources=["spotify:track:fake"], config=SpotiFLACConfig())
    report = asyncio.run(
        DownloadService(
            provider_resolver=resolver,
            provider_executor=execute,
        ).download(request)
    )

    assert report.success_count == 1
    assert first.calls == 1
    assert second.calls == 1


def test_download_service_publishes_terminal_events(monkeypatch):
    events = []

    async def fake_run(self, input_url, loop_minutes=None):
        return DownloadResult.ok("tidal", "/music/events.flac", source=input_url)

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run)
    bus = EventBus()
    bus.subscribe(
        "download.completed", lambda payload: events.append(("done", payload))
    )
    bus.subscribe("download.failed", lambda payload: events.append(("failed", payload)))
    request = DownloadRequest(
        sources=["spotify:track:abc123", "http://unsupported"],
        config=SpotiFLACConfig(),
    )

    asyncio.run(DownloadService(event_bus=bus).download(request))

    assert [kind for kind, _payload in events] == ["done", "failed"]


def test_download_service_publishes_provider_events(monkeypatch):
    events = []

    async def fake_run(self, input_url, loop_minutes=None):
        return DownloadResult.ok("tidal", "/music/provider.flac", source=input_url)

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run)
    bus = EventBus()
    for event_name in ("provider.started", "provider.succeeded"):
        bus.subscribe(event_name, lambda payload, name=event_name: events.append(name))
    request = DownloadRequest(
        sources=["spotify:track:provider"], config=SpotiFLACConfig()
    )

    asyncio.run(DownloadService(event_bus=bus).download(request))

    assert events == ["provider.started", "provider.succeeded"]


def test_queue_service_tracks_job_state():
    service = QueueService()
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    job = asyncio.run(service.enqueue(request))
    assert job["status"] == "QUEUED"
    assert job["id"]

    paused = asyncio.run(service.pause(job["id"]))
    assert paused["status"] == "PAUSED"

    resumed = asyncio.run(service.resume(job["id"]))
    assert resumed["status"] == "QUEUED"


def test_queue_service_persists_jobs_in_repository(tmp_path, monkeypatch):
    db_path = tmp_path / "queue-persistence.db"
    monkeypatch.setenv("SPOTIFLAC_DB_PATH", str(db_path))

    service = QueueService(repo=JobRepository())
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    job = asyncio.run(service.enqueue(request))
    assert job["status"] == "QUEUED"
    assert JobRepository().get(job["id"])["status"] == "QUEUED"

    paused = asyncio.run(service.pause(job["id"]))
    assert paused["status"] == "PAUSED"
    assert JobRepository().get(job["id"])["status"] == "PAUSED"


def test_metadata_service_resolves_sources_to_track_metadata():
    service = MetadataService()
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    resolved = asyncio.run(service.resolve(request))
    assert len(resolved) == 1
    assert resolved[0].id == "abc123"
    assert resolved[0].title == "Example Track"
    assert resolved[0].artists == "Example Artist"


def test_provider_resolver_prefers_supported_quality_and_fallback_order():
    resolver = ProviderResolver()
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    candidates = resolver.resolve(request)

    assert candidates[0] == "tidal"
    assert "qobuz" in candidates
    assert candidates[-1] in {"deezer", "amazon", "apple"}


def test_provider_resolver_preserves_configured_legacy_services():
    config = SpotiFLACConfig()
    config.download.services = ["ext:qobuz-web"]
    request = DownloadRequest(sources=["spotify:track:abc123"], config=config)

    assert ProviderResolver().resolve(request) == ["qobuz"]


def test_download_pipeline_prepares_source_and_provider_context():
    request = DownloadRequest(
        sources=["spotify:track:abc123"], config=SpotiFLACConfig()
    )
    pipeline = DownloadPipeline([ResolveStep(), ProviderStep(ProviderResolver())])

    context = asyncio.run(
        pipeline.prepare(DownloadContext(request, request.sources[0]))
    )

    assert context.errors == []
    assert context.provider == "tidal"
    assert context.provider_candidate is not None
    assert context.provider_candidate.name == "tidal"
    assert [candidate.name for candidate in context.provider_candidates] == [
        "tidal",
        "qobuz",
        "deezer",
        "apple",
        "amazon",
    ]


def test_download_pipeline_accepts_public_spotify_track_urls():
    request = DownloadRequest(
        sources=["https://open.spotify.com/track/abc123"],
        config=SpotiFLACConfig(),
    )
    context = asyncio.run(
        DownloadPipeline([ResolveStep()]).prepare(
            DownloadContext(request, request.sources[0])
        )
    )

    assert context.errors == []


def test_download_pipeline_publishes_namespaced_step_events():
    events = []
    bus = EventBus()
    bus.subscribe("pipeline.started", lambda payload: events.append("pipeline-start"))
    bus.subscribe("pipeline.phase_changed", lambda payload: events.append("phase"))
    bus.subscribe("pipeline.resolve.started", lambda payload: events.append("start"))
    bus.subscribe(
        "pipeline.resolve.completed", lambda payload: events.append("completed")
    )
    bus.subscribe(
        "pipeline.completed", lambda payload: events.append("pipeline-completed")
    )
    request = DownloadRequest(sources=["spotify:track:event"], config=SpotiFLACConfig())
    pipeline = DownloadPipeline(
        [ResolveStep()],
        event_bus=bus,
    )

    asyncio.run(pipeline.prepare(DownloadContext(request, request.sources[0])))

    assert events == [
        "pipeline-start",
        "phase",
        "start",
        "completed",
        "pipeline-completed",
    ]


def test_download_service_publishes_post_step_events():
    events = []
    bus = EventBus()
    bus.subscribe("pipeline.validate.started", lambda payload: events.append("start"))
    bus.subscribe(
        "pipeline.validate.completed", lambda payload: events.append("completed")
    )

    async def execute(provider, source):
        return DownloadResult.ok(provider, "/music/post-step.flac", source=source)

    request = DownloadRequest(
        sources=["spotify:track:post-step"], config=SpotiFLACConfig()
    )
    asyncio.run(
        DownloadService(event_bus=bus, provider_executor=execute).download(request)
    )

    assert events == ["start", "completed"]


def test_validate_step_rejects_an_invalid_existing_flac(tmp_path):
    path = tmp_path / "broken.flac"
    path.write_bytes(b"not flac")
    request = DownloadRequest(
        sources=["spotify:track:abc123"], config=SpotiFLACConfig()
    )
    context = DownloadContext(
        request=request,
        source=request.sources[0],
        result=DownloadResult.ok("tidal", str(path)),
    )

    context = asyncio.run(ValidateStep().execute(context))

    assert context.errors
    assert context.errors[0].startswith("validation_failed:")


def test_tag_step_delegates_to_the_tagging_boundary():
    calls = []

    async def fake_tagger(path, metadata):
        calls.append((path, metadata.title))

    request = DownloadRequest(
        sources=["spotify:track:abc123"], config=SpotiFLACConfig()
    )
    track = TrackMetadata(
        id="track-1",
        title="Song",
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )
    context = DownloadContext(
        request=request,
        source=request.sources[0],
        metadata=track,
        result=DownloadResult.ok("tidal", "/tmp/song.flac"),
    )

    asyncio.run(TagStep(fake_tagger).execute(context))

    assert calls == [("/tmp/song.flac", "Song")]


def test_lyrics_and_canvas_steps_delegate_post_processing():
    calls = []

    async def fake_lyrics(path, metadata):
        calls.append(("lyrics", path, metadata.title))

    async def fake_canvas(path, metadata):
        calls.append(("canvas", path, metadata.title))

    request = DownloadRequest(
        sources=["spotify:track:abc123"], config=SpotiFLACConfig()
    )
    track = TrackMetadata(
        id="track-1",
        title="Song",
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )
    context = DownloadContext(
        request=request,
        source=request.sources[0],
        metadata=track,
        result=DownloadResult.ok("tidal", "/tmp/song.flac"),
    )

    asyncio.run(LyricsStep(fake_lyrics).execute(context))
    asyncio.run(CanvasStep(fake_canvas).execute(context))

    assert calls == [
        ("lyrics", "/tmp/song.flac", "Song"),
        ("canvas", "/tmp/song.flac", "Song"),
    ]


def test_transcode_and_library_steps_update_context_and_index_output():
    calls = []

    async def fake_transcode(path, request):
        return path.replace(".flac", ".m4a")

    async def fake_index(path, context):
        calls.append(path)

    request = DownloadRequest(
        sources=["spotify:track:abc123"], config=SpotiFLACConfig()
    )
    context = DownloadContext(
        request=request,
        source=request.sources[0],
        result=DownloadResult.ok("tidal", "/tmp/song.flac"),
    )

    asyncio.run(TranscodeStep(fake_transcode).execute(context))
    asyncio.run(LibraryIndexStep(fake_index).execute(context))

    assert context.output_file == "/tmp/song.m4a"
    assert calls == ["/tmp/song.m4a"]


def test_provider_resolver_filters_capability_quality_and_health():
    resolver = ProviderResolver(
        [
            ProviderProfile("slow", priority=10, qualities=frozenset({"LOSSLESS"})),
            ProviderProfile(
                "hires", priority=1, qualities=frozenset({"HI_RES_LOSSLESS"})
            ),
            ProviderProfile("unhealthy", priority=100, healthy=False),
            ProviderProfile("search-only", capabilities=frozenset({"search"})),
        ]
    )
    request = DownloadRequest(sources=["spotify:track:abc"], config=SpotiFLACConfig())

    assert resolver.resolve(request) == ["slow"]
    request.config.download.quality = "HI_RES_LOSSLESS"
    assert resolver.resolve(request) == ["hires"]


def test_provider_resolver_exposes_structured_candidates():
    resolver = ProviderResolver(
        [ProviderProfile("tidal", priority=4, capabilities=frozenset({"download"}))]
    )
    request = DownloadRequest(sources=["spotify:track:abc"], config=SpotiFLACConfig())

    candidates = resolver.resolve_candidates(request)

    assert isinstance(candidates[0], ProviderCandidate)
    assert candidates[0].name == "tidal"
    assert candidates[0].priority == 4


def test_provider_profile_can_be_built_from_extension_manifest():
    profile = ProviderProfile.from_manifest(
        {
            "id": "tidal-web",
            "capabilities": {"download": True, "search": True, "metadata": False},
            "qualities": ["lossless", "hi_res_lossless"],
            "priority": 7,
        }
    )

    assert profile.name == "tidal-web"
    assert profile.capabilities == frozenset({"download", "search"})
    assert profile.qualities == frozenset({"LOSSLESS", "HI_RES_LOSSLESS"})
    assert profile.priority == 7


def test_provider_resolver_can_load_installed_extension_manifests():
    extension = SimpleNamespace(
        name="tidal-web",
        manifest={
            "capabilities": {"download": True},
            "qualities": ["LOSSLESS"],
        },
    )
    resolver = ProviderResolver.from_extensions([extension])
    request = DownloadRequest(sources=["spotify:track:abc"], config=SpotiFLACConfig())

    assert resolver.resolve(request) == ["tidal-web"]


def test_extension_manifest_is_validated_and_serializable():
    manifest = ExtensionManifest.from_dict(
        {
            "id": "tidal-web",
            "version": "2.1.0",
            "capabilities": {"download": True, "search": False},
            "qualities": ["lossless"],
        }
    )

    assert manifest.id == "tidal-web"
    assert manifest.version == "2.1.0"
    assert manifest.as_dict()["capabilities"] == {"download": True}

    with pytest.raises(ValueError, match="version"):
        ExtensionManifest.from_dict({"id": "broken"})


def test_extension_conformance_validates_manifest_and_provider_contract():
    class ReferenceProvider:
        name = "reference"

        async def download_track_async(self, *args, **kwargs):
            return DownloadResult.ok("reference", "/tmp/reference.flac")

    report = validate_extension(
        {
            "id": "reference",
            "version": "1.0.0",
            "capabilities": {"download": True},
        },
        ReferenceProvider(),
    )

    assert report.passed is True
    assert report.manifest is not None
    assert report.manifest.id == "reference"

    invalid = validate_extension(
        {"id": "metadata-only", "version": "1.0.0"},
        object(),
    )
    assert invalid.passed is False
    assert any("download capability" in error for error in invalid.errors)


def test_extension_service_tracks_lifecycle_and_trust():
    service = ExtensionService()
    installed = service.install("my-provider", version="3.1.0")
    assert installed.id == "my-provider"
    assert installed.version == "3.1.0"

    disabled = service.disable("my-provider")
    assert disabled.enabled is False

    enabled = service.enable("my-provider")
    assert enabled.enabled is True
    assert service.list()[0].id in {"tidal-web", "qobuz-web", "my-provider"}


def test_repository_layer_persists_jobs_and_extensions(tmp_path, monkeypatch):
    db_path = tmp_path / "repository-test.db"
    monkeypatch.setenv("SPOTIFLAC_DB_PATH", str(db_path))

    job_repo = JobRepository()
    extension_repo = ExtensionRepository()

    job = job_repo.create({"source": "spotify:track:abc123", "status": "QUEUED"})
    assert job["source"] == "spotify:track:abc123"
    assert job_repo.get(job["id"])["status"] == "QUEUED"

    job_repo.update_status(job["id"], "PAUSED")
    assert job_repo.get(job["id"])["status"] == "PAUSED"

    extension = extension_repo.upsert(
        {"id": "my-provider", "enabled": True, "trust": "SIGNED"}
    )
    assert extension["id"] == "my-provider"
    assert extension_repo.get("my-provider")["trust"] == "SIGNED"


def test_event_bus_dispatches_events_to_subscribers():
    bus = EventBus()
    received = []

    bus.subscribe("download.started", lambda event: received.append(event))
    asyncio.run(bus.publish("download.started", {"track_id": "abc123"}))

    assert received == [{"track_id": "abc123"}]


def test_api_adapter_builds_job_from_download_request():
    adapter = ApiAdapter()
    response = asyncio.run(
        adapter.submit_download(
            {
                "sources": ["spotify:track:abc123"],
                "quality": "HI_RES_LOSSLESS",
            }
        )
    )

    assert response["status"] == "QUEUED"
    assert response["provider_order"][0] == "tidal"
    assert response["items"] == 1


def test_api_adapter_lists_and_gets_persisted_jobs(tmp_path):
    service = JobService(repo=JobRepository(tmp_path / "adapter.db"))
    adapter = ApiAdapter(job_service=service)

    response = asyncio.run(
        adapter.submit_download({"sources": ["spotify:track:abc123"]})
    )
    jobs = asyncio.run(adapter.list_downloads())
    job = asyncio.run(adapter.get_download(response["id"]))

    assert jobs[0]["id"] == response["id"]
    assert job["status"] == "queued"
    assert job["payload"]["items"] == 1


def test_job_service_tracks_queue_and_repository_state(tmp_path, monkeypatch):
    db_path = tmp_path / "job-service.db"
    monkeypatch.setenv("SPOTIFLAC_DB_PATH", str(db_path))

    service = JobService()
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    job = asyncio.run(service.enqueue(request))
    assert job["status"] == "QUEUED"
    assert service.get(job["id"])["status"] == "QUEUED"

    asyncio.run(service.pause(job["id"]))
    assert service.get(job["id"])["status"] == "PAUSED"

    asyncio.run(service.resume(job["id"]))
    assert service.get(job["id"])["status"] == "QUEUED"


def test_job_service_executes_download_and_persists_terminal_status(tmp_path):
    class FakeDownloadService:
        async def download(self, request):
            return {"sources": request.sources}

    service = JobService(
        repo=JobRepository(tmp_path / "execution.db"),
        download_service=FakeDownloadService(),
    )
    request = DownloadRequest(
        sources=["spotify:track:abc123"],
        config=SpotiFLACConfig(),
    )

    job = asyncio.run(service.enqueue(request))
    result = asyncio.run(service.execute(job["id"]))

    assert result == {"sources": ["spotify:track:abc123"]}
    assert service.get(job["id"])["status"] == "DONE"


def test_job_service_can_cancel_and_retry_jobs(tmp_path):
    service = JobService(repo=JobRepository(tmp_path / "lifecycle.db"))
    request = DownloadRequest(
        sources=["spotify:track:abc123"], config=SpotiFLACConfig()
    )

    job = asyncio.run(service.enqueue(request))
    asyncio.run(service.cancel(job["id"]))
    assert service.get(job["id"])["status"] == "CANCELLED"
    assert asyncio.run(service.retry(job["id"]))["status"] == "QUEUED"


def test_job_service_pause_propagates_to_active_provider_and_resumes(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingExecutor(ProviderExecutor):
        async def execute(self, provider, source):
            started.set()
            await release.wait()
            self._last_result = DownloadResult.ok(
                provider, "/music/active-pause.flac", source=source
            )

    service = JobService(
        repo=JobRepository(tmp_path / "active-pause.db"),
        download_service=DownloadService(provider_executor=BlockingExecutor()),
    )
    request = DownloadRequest(
        sources=["spotify:track:active-pause"], config=SpotiFLACConfig()
    )

    async def scenario():
        job = await service.enqueue(request)
        task = asyncio.create_task(service.execute(job["id"]))
        await started.wait()
        await service.pause(job["id"])
        release.set()
        await asyncio.sleep(0)
        assert not task.done()
        assert service.get(job["id"])["status"] == "PAUSED"
        await service.resume(job["id"])
        report = await task
        return report, service.get(job["id"])

    report, job = asyncio.run(scenario())
    assert report.success_count == 1
    assert job["status"] == "DONE"


def test_job_service_retries_after_pause_cancel_and_recovers_running_items(tmp_path):
    repo = JobRepository(tmp_path / "retry-recovery.db")
    service = JobService(repo=repo, download_service=DownloadService())
    request = DownloadRequest(
        sources=["spotify:track:retry-recovery"], config=SpotiFLACConfig()
    )
    job = asyncio.run(service.enqueue(request))
    item = service.items(job["id"])[0]
    repo.update_item(item["id"], status="RUNNING", attempts=2)
    repo.update_status(job["id"], "RUNNING")
    repo.update_status(job["id"], "CANCELLED")

    asyncio.run(service.retry(job["id"]))
    assert service.items(job["id"])[0]["status"] == "QUEUED"
    assert service.items(job["id"])[0]["attempts"] == 2


def test_job_service_publishes_retrying_event(tmp_path):
    events = []
    bus = EventBus()
    bus.subscribe("job.retrying", lambda payload: events.append(payload["job_id"]))
    service = JobService(
        repo=JobRepository(tmp_path / "retrying.db"),
        event_bus=bus,
    )
    request = DownloadRequest(sources=["spotify:track:retry"], config=SpotiFLACConfig())
    job = asyncio.run(service.enqueue(request))
    asyncio.run(service.cancel(job["id"]))

    asyncio.run(service.retry(job["id"]))

    assert events == [job["id"]]


def test_job_service_reconstructs_persisted_request_after_restart(tmp_path):
    class FakeDownloadService:
        async def download(self, request):
            return request.sources

    repo = JobRepository(tmp_path / "restart.db")
    request = DownloadRequest(
        sources=["spotify:track:restart"],
        config=SpotiFLACConfig(),
    )
    first = JobService(repo=repo, download_service=FakeDownloadService())
    job = asyncio.run(first.enqueue(request))

    restarted = JobService(repo=repo, download_service=FakeDownloadService())
    result = asyncio.run(restarted.execute(job["id"]))

    assert result == ["spotify:track:restart"]
    assert restarted.get(job["id"])["status"] == "DONE"
    assert restarted.get(job["id"])["total_items"] == 1
    assert restarted.get(job["id"])["completed_items"] == 1


def test_restarted_job_can_transition_through_persistent_queue(tmp_path):
    repo = JobRepository(tmp_path / "restart-queue.db")
    request = DownloadRequest(
        sources=["spotify:track:restart-queue"],
        config=SpotiFLACConfig(),
    )
    first = JobService(repo=repo)
    job = asyncio.run(first.enqueue(request))
    restarted = JobService(repo=repo)

    paused = asyncio.run(restarted.pause(job["id"]))
    resumed = asyncio.run(restarted.resume(job["id"]))
    cancelled = asyncio.run(restarted.cancel(job["id"]))

    assert paused["status"] == "PAUSED"
    assert resumed["status"] == "QUEUED"
    assert cancelled["status"] == "CANCELLED"


def test_paused_job_does_not_start_until_resumed(tmp_path):
    calls = []

    class FakeDownloadService:
        async def download(self, request):
            calls.append(request.sources)
            return DownloadReport(succeeded=[], failed=[], skipped=[])

    repo = JobRepository(tmp_path / "paused-execution.db")
    service = JobService(repo=repo, download_service=FakeDownloadService())
    request = DownloadRequest(
        sources=["spotify:track:paused"], config=SpotiFLACConfig()
    )
    job = asyncio.run(service.enqueue(request))

    asyncio.run(service.pause(job["id"]))
    assert asyncio.run(service.execute(job["id"])) is None
    assert calls == []

    asyncio.run(service.resume(job["id"]))
    asyncio.run(service.execute(job["id"]))
    assert calls == [["spotify:track:paused"]]


def test_retry_requeues_terminal_job_items(tmp_path):
    repo = JobRepository(tmp_path / "retry-items.db")
    service = JobService(repo=repo)
    request = DownloadRequest(sources=["spotify:track:retry"], config=SpotiFLACConfig())
    job = asyncio.run(service.enqueue(request))
    item = service.items(job["id"])[0]
    repo.update_item(
        item["id"],
        status="FAILED",
        attempts=2,
        provider="tidal",
        error="temporary",
    )
    repo.update_status(job["id"], "FAILED")

    asyncio.run(service.retry(job["id"]))

    retried = service.items(job["id"])[0]
    assert retried["status"] == "QUEUED"
    assert retried["attempts"] == 2
    assert retried["provider"] is None
    assert retried["error"] is None


def test_retry_clears_stale_job_lifecycle_metadata(tmp_path):
    repo = JobRepository(tmp_path / "retry-metadata.db")
    service = JobService(repo=repo)
    job = asyncio.run(
        service.enqueue(
            DownloadRequest(
                sources=["spotify:track:retry-meta"], config=SpotiFLACConfig()
            )
        )
    )
    repo.update_status(job["id"], "RUNNING")
    repo.update_status(job["id"], "FAILED")
    repo.update_error(job["id"], "old failure")

    asyncio.run(service.retry(job["id"]))
    retried = service.get(job["id"])

    assert retried["status"] == "QUEUED"
    assert retried["finished_at"] is None
    assert retried["error"] is None


def test_job_service_publishes_lifecycle_events(tmp_path):
    events = []
    bus = EventBus()
    for event_name in ("job.created", "job.started", "job.completed"):
        bus.subscribe(event_name, lambda payload, name=event_name: events.append(name))

    class FakeDownloadService:
        async def download(self, request):
            return request.sources

    service = JobService(
        repo=JobRepository(tmp_path / "events.db"),
        download_service=FakeDownloadService(),
        event_bus=bus,
    )
    request = DownloadRequest(sources=["spotify:track:event"], config=SpotiFLACConfig())
    job = asyncio.run(service.enqueue(request))
    asyncio.run(service.execute(job["id"]))

    assert events == ["job.created", "job.started", "job.completed"]


def test_job_service_cancels_active_download_task(tmp_path):
    started = asyncio.Event()

    class SlowDownloadService:
        async def download(self, request):
            started.set()
            await asyncio.Future()

    service = JobService(
        repo=JobRepository(tmp_path / "cancel-active.db"),
        download_service=SlowDownloadService(),
    )
    request = DownloadRequest(
        sources=["spotify:track:cancel"], config=SpotiFLACConfig()
    )

    async def scenario():
        job = await service.enqueue(request)
        task = asyncio.create_task(service.execute(job["id"]))
        await started.wait()
        await service.cancel(job["id"])
        try:
            await task
        except asyncio.CancelledError:
            pass
        return service.get(job["id"])

    assert asyncio.run(scenario())["status"] == "CANCELLED"


def test_job_service_persists_execution_attempts(tmp_path):
    class FakeDownloadService:
        async def download(self, request):
            return request.sources

    repo = JobRepository(tmp_path / "attempts.db")
    service = JobService(repo=repo, download_service=FakeDownloadService())
    request = DownloadRequest(
        sources=["spotify:track:attempt"], config=SpotiFLACConfig()
    )
    job = asyncio.run(service.enqueue(request))

    asyncio.run(service.execute(job["id"]))

    attempts = repo.list_attempts(job["id"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "COMPLETED"
    assert attempts[0]["finished_at"] is not None


def test_job_service_persists_one_item_per_source(tmp_path):
    repo = JobRepository(tmp_path / "items.db")
    service = JobService(repo=repo)
    request = DownloadRequest(
        sources=["spotify:track:item-a", "spotify:track:item-b"],
        config=SpotiFLACConfig(),
    )

    job = asyncio.run(service.enqueue(request))
    items = service.items(job["id"])

    assert [item["source"] for item in items] == request.sources
    assert [item["status"] for item in items] == ["QUEUED", "QUEUED"]
    assert all(item["attempts"] == 0 for item in items)


def test_job_service_updates_item_results_by_source(tmp_path):
    class FakeDownloadService:
        async def download(self, request):
            return DownloadReport(
                succeeded=[
                    DownloadResult.ok("tidal", "/tmp/a.flac", source=request.sources[0])
                ],
                failed=[
                    DownloadFailure(
                        source=request.sources[1],
                        reason="not_found",
                        provider="tidal",
                        attempts=2,
                    )
                ],
                skipped=[],
            )

    repo = JobRepository(tmp_path / "item-results.db")
    service = JobService(repo=repo, download_service=FakeDownloadService())
    request = DownloadRequest(
        sources=["spotify:track:done", "spotify:track:failed"],
        config=SpotiFLACConfig(),
    )
    job = asyncio.run(service.enqueue(request))
    asyncio.run(service.execute(job["id"]))

    items = service.items(job["id"])
    assert items[0]["status"] == "DONE"
    assert items[0]["attempts"] == 1
    assert items[0]["result"] == "/tmp/a.flac"
    assert items[1]["status"] == "FAILED"
    assert items[1]["error"] == "not_found"
    assert items[1]["attempts"] == 2


def test_job_service_marks_report_failures_as_failed(tmp_path):
    class FakeDownloadService:
        async def download(self, request):
            return DownloadReport(
                succeeded=[],
                failed=[
                    DownloadFailure(
                        source=request.sources[0],
                        reason="provider_timeout",
                        provider="tidal",
                        attempts=3,
                    )
                ],
                skipped=[],
            )

    repo = JobRepository(tmp_path / "report-failure.db")
    service = JobService(repo=repo, download_service=FakeDownloadService())
    job = asyncio.run(
        service.enqueue(
            DownloadRequest(
                sources=["spotify:track:report-failure"], config=SpotiFLACConfig()
            )
        )
    )

    asyncio.run(service.execute(job["id"]))

    stored = service.get(job["id"])
    assert stored["status"] == "FAILED"
    assert stored["error"] == "provider_timeout"


def test_job_service_publishes_item_updates(tmp_path):
    events = []
    lifecycle = []
    bus = EventBus()
    bus.subscribe("job.item.updated", lambda payload: events.append(payload))
    bus.subscribe("job.item.created", lambda payload: lifecycle.append("created"))
    bus.subscribe("job.item.completed", lambda payload: lifecycle.append("completed"))

    class FakeDownloadService:
        async def download(self, request):
            return DownloadReport(
                succeeded=[
                    DownloadResult.ok(
                        "tidal", "/tmp/item.flac", source=request.sources[0]
                    )
                ],
                failed=[],
                skipped=[],
            )

    service = JobService(
        repo=JobRepository(tmp_path / "item-events.db"),
        download_service=FakeDownloadService(),
        event_bus=bus,
    )
    job = asyncio.run(
        service.enqueue(
            DownloadRequest(
                sources=["spotify:track:item-event"], config=SpotiFLACConfig()
            )
        )
    )
    asyncio.run(service.execute(job["id"]))

    assert [event["item"]["status"] for event in events] == ["DONE"]
    assert events[0]["job_id"] == job["id"]
    assert lifecycle == ["created", "completed"]


def test_job_repository_persists_lifecycle_timestamps(tmp_path):
    repo = JobRepository(tmp_path / "job-timestamps.db")
    job = repo.create({"id": "job-time", "source": "track", "status": "QUEUED"})

    created = repo.get(job["id"])
    repo.update_status(job["id"], "RUNNING")
    running = repo.get(job["id"])
    repo.update_status(job["id"], "FAILED")
    repo.update_error(job["id"], "provider unavailable")
    failed = repo.get(job["id"])

    assert created["created_at"] is not None
    assert running["started_at"] is not None
    assert failed["finished_at"] is not None
    assert failed["error"] == "provider unavailable"


def test_job_repository_migrates_legacy_job_schema(tmp_path):
    db_path = tmp_path / "legacy-jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE application_jobs ("
            "id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL, "
            "payload TEXT NOT NULL DEFAULT '{}', priority INTEGER NOT NULL DEFAULT 0, "
            "total_items INTEGER NOT NULL DEFAULT 0, "
            "completed_items INTEGER NOT NULL DEFAULT 0)"
        )

    repo = JobRepository(db_path)
    repo.create({"id": "legacy-job", "source": "track", "status": "QUEUED"})
    migrated = repo.get("legacy-job")

    assert migrated["created_at"] is not None
    assert migrated["started_at"] is None
    assert migrated["finished_at"] is None
    assert migrated["error"] is None


def test_api_adapter_shares_event_bus_with_job_service(tmp_path):
    events = []
    bus = EventBus()
    bus.subscribe("job.created", lambda payload: events.append(payload["job_id"]))
    service = JobService(
        repo=JobRepository(tmp_path / "adapter-events.db"), event_bus=bus
    )
    adapter = ApiAdapter(job_service=service, event_bus=bus)

    response = asyncio.run(
        adapter.submit_download({"sources": ["spotify:track:event"]})
    )

    assert events == [response["id"]]


def test_api_adapter_exposes_job_item_details(tmp_path):
    service = JobService(repo=JobRepository(tmp_path / "adapter-items.db"))
    adapter = ApiAdapter(job_service=service)

    response = asyncio.run(
        adapter.submit_download(
            {"sources": ["spotify:track:item-detail"], "quality": "LOSSLESS"}
        )
    )
    job = asyncio.run(adapter.get_download(response["id"]))

    assert job is not None
    assert job["payload"]["items_detail"][0]["source"] == "spotify:track:item-detail"
    assert job["payload"]["items_detail"][0]["status"] == "QUEUED"


def test_job_response_schema_accepts_persisted_lifecycle_states():
    from SpotiFLAC.webapi.schemas import JobOut

    for status in ("paused", "retrying", "cancelled"):
        job = JobOut(
            id=f"job-{status}",
            status=status,
            created_at=0.0,
        )
        assert job.status == status


def test_v1_routes_control_application_job_lifecycle():
    app = FastAPI()
    app.include_router(
        build_v1_router(
            ApiDeps(
                api_for=lambda _request: SimpleNamespace(download_dir="/downloads"),
                adapter=ApiAdapter(),
            )
        )
    )
    client = TestClient(app)

    created = client.post(
        "/api/v1/downloads",
        json={"url": "spotify:track:controlled"},
    )
    job_id = created.json()["id"]

    paused = client.post(f"/api/v1/downloads/{job_id}/pause")
    resumed = client.post(f"/api/v1/downloads/{job_id}/resume")

    assert created.status_code == 202
    assert created.json()["payload"]["items_detail"][0]["status"] == "QUEUED"
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "queued"
    retry = client.post(f"/api/v1/downloads/{job_id}/retry")
    assert retry.status_code == 409

    second = client.post(
        "/api/v1/downloads",
        json={"url": "spotify:track:cancelled"},
    )
    second_id = second.json()["id"]
    cancelled = client.post(f"/api/v1/downloads/{second_id}/cancel")
    retried = client.post(f"/api/v1/downloads/{second_id}/retry")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"


def test_v1_router_uses_application_adapter_for_download_submission():
    app = FastAPI()
    app.include_router(
        build_v1_router(
            ApiDeps(
                api_for=lambda request: SimpleNamespace(app_version="test"),
                adapter=ApiAdapter(),
            )
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/downloads",
            json={"url": "https://example.com/track", "quality": "HI_RES_LOSSLESS"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["payload"]["provider_order"][0] == "tidal"

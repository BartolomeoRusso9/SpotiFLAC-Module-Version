import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
    MetadataService,
    OutputService,
    ProviderResolver,
    QueueService,
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
from SpotiFLAC.core.providers import ProviderProfile
from SpotiFLAC.core.retry import RetryPolicy
from SpotiFLAC.client import AsyncSpotiFLAC
from SpotiFLAC.webapi import ApiDeps, build_v1_router
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
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


def test_download_service_runs_injected_post_processors(monkeypatch):
    calls = []

    async def fake_run_async(self, input_url, loop_minutes=None):
        return None

    async def fake_tagger(path, metadata):
        calls.append(("tag", path, metadata.title))

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run_async)
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
    assert calls == [("tag", "/tmp/abc123.flac", "Song")]


def test_download_service_resolves_metadata_for_post_processors(monkeypatch):
    calls = []

    async def fake_run_async(self, input_url, loop_minutes=None):
        return None

    async def fake_tagger(path, metadata):
        calls.append(metadata.title)

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run_async)
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
    request = DownloadRequest(sources=["spotify:track:abc123"], config=SpotiFLACConfig())

    report = asyncio.run(client.download_request(request))

    assert report.success_count == 1
    assert client.application_config().output.directory == Path("downloads")


def test_download_service_publishes_terminal_events(monkeypatch):
    events = []

    async def fake_run(self, input_url, loop_minutes=None):
        return None

    monkeypatch.setattr("SpotiFLAC.downloader.SpotiflacDownloader.run_async", fake_run)
    bus = EventBus()
    bus.subscribe("download.completed", lambda payload: events.append(("done", payload)))
    bus.subscribe("download.failed", lambda payload: events.append(("failed", payload)))
    request = DownloadRequest(
        sources=["spotify:track:abc123", "http://unsupported"],
        config=SpotiFLACConfig(),
    )

    asyncio.run(DownloadService(event_bus=bus).download(request))

    assert [kind for kind, _payload in events] == ["done", "failed"]


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


def test_download_pipeline_prepares_source_and_provider_context():
    request = DownloadRequest(sources=["spotify:track:abc123"], config=SpotiFLACConfig())
    pipeline = DownloadPipeline([ResolveStep(), ProviderStep(ProviderResolver())])

    context = asyncio.run(pipeline.prepare(DownloadContext(request, request.sources[0])))

    assert context.errors == []
    assert context.provider == "tidal"


def test_validate_step_rejects_an_invalid_existing_flac(tmp_path):
    path = tmp_path / "broken.flac"
    path.write_bytes(b"not flac")
    request = DownloadRequest(sources=["spotify:track:abc123"], config=SpotiFLACConfig())
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

    request = DownloadRequest(sources=["spotify:track:abc123"], config=SpotiFLACConfig())
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

    request = DownloadRequest(sources=["spotify:track:abc123"], config=SpotiFLACConfig())
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

    request = DownloadRequest(sources=["spotify:track:abc123"], config=SpotiFLACConfig())
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
            ProviderProfile("hires", priority=1, qualities=frozenset({"HI_RES_LOSSLESS"})),
            ProviderProfile("unhealthy", priority=100, healthy=False),
            ProviderProfile("search-only", capabilities=frozenset({"search"})),
        ]
    )
    request = DownloadRequest(sources=["spotify:track:abc"], config=SpotiFLACConfig())

    assert resolver.resolve(request) == ["slow"]
    request.config.download.quality = "HI_RES_LOSSLESS"
    assert resolver.resolve(request) == ["hires"]


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

    extension = extension_repo.upsert({"id": "my-provider", "enabled": True, "trust": "SIGNED"})
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
    request = DownloadRequest(sources=["spotify:track:abc123"], config=SpotiFLACConfig())

    job = asyncio.run(service.enqueue(request))
    asyncio.run(service.cancel(job["id"]))
    assert service.get(job["id"])["status"] == "CANCELLED"
    assert asyncio.run(service.retry(job["id"]))["status"] == "QUEUED"


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

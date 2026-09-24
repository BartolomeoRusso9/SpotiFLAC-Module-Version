# Refactor Diff Report

## Summary

- Foundation and incremental refactor changes are committed on branch `4.0.0`.
- Local test output `.spotiflac/` remains intentionally untracked.
- `git diff --check`: passed
- Focused regression suite: **151 passed** across the architecture and web
  boundary suites.

## Modified Files

### `SpotiFLAC/__init__.py`

Exports the new public contracts:

- `SpotiFLACConfig`
- `DownloadRequest`
- `DownloadReport`
- `DownloadFailure`
- `DownloadSkip`

### `SpotiFLAC/core/__init__.py`

Re-exports the configuration and download contracts from the core namespace.

### `SpotiFLAC/webapi/routes.py`

Adds application-adapter support for:

- creating downloads;
- listing application jobs;
- retrieving jobs by ID;
- preserving the legacy multi-user queue and quota checks.

### `SpotiFLAC/webapp.py`

Connects the versioned REST API to `ApiAdapter` and the persistent download queue.

## New Modules

- `SpotiFLAC/application/`
  - application services;
  - download pipeline;
  - job lifecycle;
  - output handling;
  - retry policy;
  - provider resolver.
- `SpotiFLAC/core/config/`
  - structured configuration;
  - request, report, failure, and skip contracts.
- `SpotiFLAC/core/providers/`
  - manifest-derived provider profiles;
  - capability, quality, health, and priority filtering;
  - structured `ProviderCandidate` results.
- `SpotiFLAC/core/repositories/`
  - SQLite repositories for application jobs and extensions.
- Application jobs reconstruct persisted `DownloadRequest` objects after a
  restart, and job IDs are independent of in-memory queue length.
- Application jobs persist `priority`, `total_items`, and `completed_items`,
  with additive schema migration for existing job databases.
- Application job attempts are persisted with status, timestamps, and errors.
- Cancelling an active application job now cancels its asyncio task, persists
  `CANCELLED`, and publishes the corresponding lifecycle event.
- Retrying a failed or cancelled job now emits `job.retrying` and records the
  intermediate `RETRYING` state before re-queueing it.
- `JobService` publishes lifecycle events through `EventBus`: created, started,
  completed, failed, and cancelled.
- The REST adapter shares the application bus with the WebSocket bridge, which
  forwards job lifecycle events as `applicationEvent` messages.
- `DownloadService` publishes provider lifecycle events: started, succeeded,
  and failed.
- `ProviderResolver.from_extensions()` can load profiles from installed
  extension manifests without coupling the core resolver to `ExtensionManager`.
- `ExtensionManifest` now provides a validated, serializable capability contract;
  legacy partial manifests remain supported through compatibility adapters.
- `DownloadContext` now carries the full ordered provider candidate chain;
  provider lifecycle events expose that chain to adapters.
- `DownloadService` can execute candidates in order through an injected provider
  executor, falling back to the next candidate after retryable failures.
- `ProviderExecutor` centralizes per-provider retry execution through
  `RetryPolicy`; callable executors remain supported for compatibility.
- Callable provider executors are normalized to `ProviderExecutor` at service
  construction, so `DownloadService` has one execution and retry path.
- `ProviderExecutor` applies the request timeout around provider execution;
  timeout failures remain classified through the shared retry policy.
- A DownloadService integration regression verifies timeout propagation and
  source-aware failure reporting.
- Provider error classification is exposed by `ProviderExecutor`, keeping
  retryability decisions at the provider execution boundary.
- `RetryPolicy` now honors the existing typed provider errors, retrying network
  rate-limit, and unavailable-provider failures while stopping on terminal
  errors such as missing tracks.
- An architecture regression guard ensures only `LegacyDownloadAdapter` imports
  the legacy downloader from the application package.
- Pipeline preparation steps publish namespaced `pipeline.<step>.started`,
  `.completed`, and `.failed` events without colliding with provider lifecycle
  events.
- The same namespaced lifecycle events now cover validation, tagging, lyrics,
  canvas, transcoding, and library indexing post-steps.
- `DownloadPipeline` owns both preparation and post-processing execution, so
  `DownloadService` no longer maintains a second step orchestration loop.
- Persistent `JobItem` records now exist for each queued source, with status,
  attempts, provider, error, result, and timestamps.
- `DownloadResult` and `DownloadFailure` carry optional source identity, so
  `JobService` updates terminal JobItem state deterministically by source.
- JobItems now transition through `RUNNING` with attempt tracking and are
  finalized as `DONE`, `FAILED`, or `CANCELLED` with the corresponding error
  and provider data.
- REST job views expose persisted item details through the existing payload,
  preserving the current `JobOut` response shape.
- The `202 Accepted` creation response also includes the initial `JobItem`
  snapshots, so clients can render queued sources without an extra poll.
- Queue pause, resume, and cancellation transitions now recover jobs from
  SQLite after a process restart instead of requiring in-memory state.
- Paused jobs are prevented from starting until resumed, with dedicated
  `job.paused` and `job.resumed` lifecycle events.
- Cooperative pause now propagates through `JobService`, `DownloadService`,
  `DownloadPipeline`, and `ProviderExecutor` using an async resume gate.
  Safe boundaries are honored before items, pipeline phases, retries, and
  after provider completion.
- Retrying a failed or cancelled job requeues its terminal JobItems, clears
  stale provider/error/result fields, and preserves the accumulated attempt
  count.
- REST `JobOut` now accepts the full persisted lifecycle state set, including
  `paused`, `retrying`, and `cancelled`.
- REST v1 application jobs now expose lifecycle controls for pause, resume,
  cancel, and retry through the shared `ApiAdapter` and `JobService`.
- The HTTP regression path covers the complete control cycle, including
  cancelling a job and retrying it back into the queue.
- Invalid lifecycle transitions are returned as structured HTTP `409 Conflict`
  responses instead of leaking application exceptions as server errors.
- The shared REST response metadata declares `409` with the same
  `ErrorResponse` model, keeping OpenAPI aligned with runtime behavior.
- JobService publishes `job.item.updated` with the persisted item snapshot,
  allowing UI and WebSocket consumers to follow per-source progress.
- The webapp WebSocket bridge forwards item updates and job pause/resume/retry
  events through the existing `applicationEvent` protocol.
- Job repository records now persist `created_at`, `started_at`, `finished_at`,
  and job-level errors; REST views read these durable values instead of
  synthesizing timestamps on every request.
- A structured download report containing failures now marks the job `FAILED`
  and persists the first failure reason, while the technical attempt remains
  `COMPLETED` because execution itself finished.
- Retrying a job also clears stale job-level error and finish-time metadata
  before it returns to the queue.
- The repository migration is covered from the legacy `application_jobs`
  schema, preserving existing databases while adding lifecycle metadata.
- Explicitly prefetched metadata now flows through `LegacyDownloadAdapter` to
  the downloader track runner, avoiding a second metadata resolution.
- A network-free fake provider fixture covers deterministic fallback behavior.
- GUI batch, folder, Hi-Res, prefetched-selection, and post-action tests now
  observe `DownloadService` and `DownloadRequest` instead of the removed
  top-level legacy wrapper.
- TUI configuration serialization now excludes CLI-only reporting and library
  notification fields that guided execution does not consume.
- `LegacyDownloadAdapter` scopes structured legacy options to the provider
  candidate selected by `ProviderResolver`; the legacy engine retains only
  provider-runtime compatibility within that selected service.
- Structured `DownloadResult` values returned by the legacy engine now flow
  through `LegacyDownloadAdapter` into `DownloadReport` without filesystem
  path inference; legacy engines that expose no result now fail explicitly.
- Production adapters created by `DownloadService.from_legacy_options()` now
  reject unstructured legacy results; every provider path must return a typed
  `DownloadResult`.
- `DownloadWorker` exposes typed per-track results, allowing the application
  adapter to consume the actual output path after a legacy run.
- `PostProcessingService` now owns the post-download phase boundary, including
  transcode, lyric/canvas sidecars, and post-download hooks; legacy helpers are
  injected behind that boundary while their lower-level extraction continues.
- `ApplicationDownloadWorker` now owns batch concurrency, result collection,
  queue callbacks, skips, and failures; `LegacyDownloadWorker` remains only
  as the compatibility wrapper for provider and filesystem helpers.
- `DownloadService` no longer synthesizes a result when a provider returns no
  structured result; the adapter reports the contract violation explicitly.
- Normal `ProviderExecutor` callbacks must now return an explicit
  `DownloadResult` or no result; no-result execution is reported as failure.
- Skipped provider results preserve their source and populate
  `DownloadReport.skipped` instead of being counted as successful downloads.
- `DownloadService` publishes `provider.selected` before provider execution.
- `DownloadPipeline` publishes `pipeline.started`, `pipeline.phase_changed`,
  and terminal `pipeline.completed`/`pipeline.failed` events around its
  existing namespaced step events.
- `JobService` publishes explicit `job.item.created`, `.completed`,
  `.failed`, and `.cancelled` events alongside the existing update event.
- Architecture guards now prevent TUI legacy-downloader access and application
  imports of UI/web implementation modules.
- `SpotiFLAC.extensions.conformance` provides a network-free manifest/provider
  contract validator with a reference-provider regression test.
- `JobRepository` now commits and closes each SQLite connection explicitly,
  eliminating leaked database handles in web tests.
- PWA tests close `TestClient` instances explicitly and pass with
  `ResourceWarning` treated as an error.
- `SpotiFLAC/core/retry.py`
  - centralized retry policy.
- `tests/test_architecture_foundation.py`
  - regression coverage for the new architecture.
- `docs/architecture/legacy-downloader-audit.md`
  - read-only classification of legacy entry points, responsibilities,
    replacements, and the next safe extraction slice.
- `DownloadConfig.services` now preserves legacy provider configuration and
  `ProviderResolver` enforces it when selecting candidates.
- `ResolveStep` accepts public Spotify track URLs in addition to Spotify URNs.

### Client Entry Point

`AsyncSpotiFLAC.download_request()` now delegates directly to
`DownloadService`, while the existing `download_track()`, `download_batch()`,
and `download_tracks()` methods remain unchanged for compatibility.

The async client constructs its shared service through
`DownloadService.from_legacy_options()`, keeping the legacy downloader handle
owned by `LegacyDownloadAdapter` while preserving the existing convenience
method contracts.

The CLI simple-URL path without `--loop` delegates to `DownloadService`, with
the legacy engine constructed only inside `LegacyDownloadAdapter`. CSV,
playlist, and loop modes retain their specialized compatibility paths.

## Pipeline

```text
DownloadRequest
    -> MetadataService
    -> ResolveStep
    -> ProviderStep
    -> LegacyDownloadAdapter
    -> Legacy Downloader
    -> ValidateStep
    -> TagStep
    -> LyricsStep
    -> CanvasStep
    -> TranscodeStep
    -> LibraryIndexStep
    -> DownloadReport
```

## Verification

Focused regression command:

```bash
PYTHONPATH="$PWD" python3 -m pytest -q \
  tests/test_architecture_foundation.py \
  tests/test_webapi_v1.py \
  tests/test_webapp_isolation.py
```

Result: **154 passed**.

The expanded GUI, TUI, architecture, and web regression set passes **224
tests** with `git diff --check` clean.

The latest architecture, API, isolation, and PWA run passes **154 tests**.
The full suite most recently passed **2091 tests** with 2 skips. Ruff and
Black pass repository-wide. Focused Mypy is clean for the refactored
application and job repository boundary. Package build, wheel installation
smoke testing, and public-contract imports pass for version 4.3.1.

## Foundation Status

The foundation and P0 contracts are complete for the incremental refactor:

- structured configuration and download contracts;
- application download entry points;
- provider profiles, candidates, capabilities, and fallback;
- extension manifest contract;
- persistent jobs, progress, retry, cancellation, and lifecycle events;
- REST and WebSocket adapters;
- CLI, GUI, Python client, and web queue entry points routed through
  `DownloadService.download()`;
- CLI standard and GUI paths construct the legacy engine only through
  `LegacyDownloadAdapter`;
- one shared web application download service for the v1 router and queue;
- focused architecture regression coverage.
- structured reports without application-level synthetic result paths;
- cooperative active-work pause and deterministic retry/recovery;
- Docker build, runtime health, and graceful shutdown validation.

The remaining work is broader decomposition of legacy-only implementation
details and cleanup of the global Mypy backlog outside the refactored
application boundary.

CI now runs the architecture guard suite, package build, and a blocking Docker
build/runtime health job as explicit gates, in addition to the existing tests,
coverage, Ruff, Black, Mypy advisory, Windows, and frontend syntax checks.

## Progress Estimate

The incremental refactor is now approximately **95% complete overall**:

- Foundation/P0: complete.
- Core P1 application and job architecture: complete for the current public
  interfaces.
- Remaining: broader legacy-engine decomposition and global Mypy cleanup
  outside the refactored application boundary. Ruff, Black, package build,
  wheel smoke validation, Docker validation, and focused architecture tests
  are green.

## Next Milestone

The next architectural step is to finish reducing the legacy downloader to an
internal compatibility engine while preserving the application entry point
used by every public interface:

```text
CLI / TUI / GUI / REST / Python
              -> DownloadService
              -> DownloadPipeline
              -> Legacy Downloader adapter
```

The remaining extraction is limited to legacy-only implementation details.
Metadata resolution, provider execution, structured reports, job handling,
active pause propagation, and Docker release validation now have application-
level ownership. Construction and execution remain behind
`LegacyDownloadAdapter`; no public API breaking change is required.

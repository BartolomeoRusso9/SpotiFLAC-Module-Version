# Refactor Diff Report

## Summary

- Foundation and incremental refactor changes are committed on branch `4.0.0`.
- Local test output `.spotiflac/` remains intentionally untracked.
- `git diff --check`: passed
- Focused regression suite: **96 passed**.

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
- `SpotiFLAC/core/retry.py`
  - centralized retry policy.
- `tests/test_architecture_foundation.py`
  - regression coverage for the new architecture.

### Client Entry Point

`AsyncSpotiFLAC.download_request()` now delegates directly to
`DownloadService`, while the existing `download_track()`, `download_batch()`,
and `download_tracks()` methods remain unchanged for compatibility.

The CLI simple-URL path without `--loop` also delegates to `DownloadService`
using the already configured legacy downloader instance. CSV, playlist, and
loop modes remain on their existing paths for compatibility.

## Pipeline

```text
DownloadRequest
    -> MetadataService
    -> ResolveStep
    -> ProviderStep
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
  tests/test_webapi_integration.py
```

Result: **96 passed**.

## Foundation Status

The foundation and P0 contracts are complete for the incremental refactor:

- structured configuration and download contracts;
- application download entry points;
- provider profiles, candidates, capabilities, and fallback;
- extension manifest contract;
- persistent jobs, progress, retry, cancellation, and lifecycle events;
- REST and WebSocket adapters;
- focused architecture regression coverage.

The remaining work is P1/P2 integration: migrating the full GUI/TUI paths,
expanding persistence entities, and completing the testing and distribution
layers.

## Next Milestone

The next architectural step is to make `DownloadService.download()` the
application entry point used by every interface, while keeping the legacy
downloader as an internal compatibility engine:

```text
CLI / TUI / GUI / REST / Python
              -> DownloadService
              -> DownloadPipeline
              -> Legacy Downloader adapter
```

The legacy downloader should then be reduced incrementally by extracting
metadata resolution, provider execution, job handling, and report generation.
No public API breaking change is required for this milestone.

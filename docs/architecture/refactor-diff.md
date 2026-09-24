# Refactor Diff Report

## Summary

- Tracked files modified: 4
- Tracked insertions: 58
- Tracked deletions: 1
- New application and test files are currently untracked.
- `git diff --check`: passed
- Focused regression suite: **86 passed**.

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
  - provider profiles;
  - capability, quality, health, and priority filtering.
- `SpotiFLAC/core/repositories/`
  - SQLite repositories for application jobs and extensions.
- Application jobs now reconstruct persisted `DownloadRequest` objects after a
  restart, and job IDs are independent of in-memory queue length.
- `SpotiFLAC/core/retry.py`
  - centralized retry policy.
- `SpotiFLAC/core/providers/`
  - manifest-derived provider profiles, capability-aware resolution, and
    `ProviderCandidate`.
- `tests/test_architecture_foundation.py`
  - regression coverage for the new architecture.

### Client Entry Point

`AsyncSpotiFLAC.download_request()` now delegates directly to
`DownloadService`, while the existing `download_track()`, `download_batch()`,
and `download_tracks()` methods remain unchanged for compatibility.

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

Result: **86 passed**.

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

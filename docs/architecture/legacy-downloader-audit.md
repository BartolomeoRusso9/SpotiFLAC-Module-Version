# Legacy Downloader Audit

Status: read-only audit completed 2026-09-25. No legacy code is removed by this audit.

| Component | Used by | Required? | Replacement | Action |
| --- | --- | --- | --- | --- |
| `SpotiflacDownloader.run_async()` | CLI loop mode and compatibility client flows | Yes, compatibility-only | `DownloadService.download()` for single structured requests | Preserve until loop mode is migrated |
| `SpotiflacDownloader.run_tracks_async()` | GUI batch selection and `AsyncSpotiFLAC.download_tracks()` | Yes, compatibility-only | Application batch service with complete metadata support | Preserve short term; migrate with batch regression coverage |
| `run_playlists_async()` / `run_csv_async()` | CLI playlist and CSV modes | Yes | No complete replacement yet; playlist sync, M3U, dedup, and indexing remain legacy-specific | Keep isolated behind compatibility entry points |
| `DownloadWorker` / `download_one_async()` | Legacy provider execution and post-processing | Yes for current runtime | `ProviderExecutor` plus `DownloadPipeline` | Keep until real provider execution returns structured results for every path |
| `_resolve_metadata_async()` | Client playlist/CSV and legacy collection flows | Yes for current compatibility paths | A real application `MetadataService` | Do not remove until HTTP Spotify and non-Spotify source coverage exists |
| `_build_providers_for_name()` | Legacy worker/provider construction | Yes for legacy paths | `ProviderResolver` plus `application.provider_factory` | Compatibility delegate only; ranking remains in `ProviderResolver` |
| `ProviderResolver` | `DownloadPipeline` and `DownloadService` | Yes | Canonical provider selection | Structured `services` configuration is now preserved and enforced |
| `ProviderExecutor` | `DownloadService` and injected providers | Yes | Stable execution contract returning `DownloadResult` | Normal execution now consumes explicit results; synthetic success is removed |
| `LegacyDownloadAdapter` | CLI, GUI, client, and compatibility service construction | Yes, temporary | Real provider executor | Keep as the only application-to-legacy boundary |
| Legacy worker post-processing | Validation, tagging, lyrics, canvas, transcoding, hooks | Current compatibility requirement | `DownloadPipeline` post steps | Post-processing and batch finalization now have application-owned services; provider/metadata execution remains legacy |
| Legacy metadata helpers | `_call_metadata_get_url()`, JS response adaptation, enrichment helpers | Yes currently | Shared metadata boundary | Move only after equivalent behavior is covered |
| Legacy report/result paths | Worker return values and typed adapter results | Done | `DownloadResult` -> `LegacyDownloadAdapter` -> `DownloadReport` | Unstructured results fail explicitly; skipped results preserve source and populate `DownloadReport.skipped` |
| Legacy collection state | Playlist/CSV/loop bookkeeping and progress | Yes for supported legacy entry points | Application batch/job contracts where available | Preserve until public compatibility paths have replacements |

`BatchFinalizer` now owns cleanup of partial output files and the configured
post-batch actions. The legacy worker retains compatibility delegates while
provider execution, collection metadata resolution, and playlist/CSV/loop
bookkeeping remain the next extraction slices.

## Entry Points

- Public exports: `DownloadOptions`, `SpotiflacDownloader` in `SpotiFLAC/__init__.py`.
- Core execution: `download_one_async()` and `DownloadWorker.run_async()` in `SpotiFLAC/downloader.py`.
- Collection execution: `run_async()`, `run_tracks_async()`, `run_playlists_async()`, and `run_csv_async()`.
- Compatibility metadata: `_resolve_metadata_async()` and `_resolve_isrc_bulk_async()`.
- Application boundary: `ApiAdapter` -> `JobService` -> `DownloadService` -> `LegacyDownloadAdapter`.
- Public compatibility callers remain in `client.py`, `launcher.py`, `api_mixins/csv_import.py`, and selected GUI/TUI paths.

## Findings

1. The application `MetadataService` is not yet a full replacement: it currently handles only synthetic Spotify track metadata for URN sources.
2. The application resolve boundary now accepts both Spotify URNs and public `open.spotify.com` URLs, but URL metadata extraction still needs a real resolver implementation.
3. Provider configuration was previously lost during `DownloadOptions` conversion; `DownloadConfig.services` now preserves it and `ProviderResolver` filters candidates accordingly.
4. Legacy provider execution still owns real network/provider matching and several post-processing operations. Removing `DownloadWorker` before migrating those contracts would break supported compatibility flows.
5. The application report path no longer fabricates normal results. Legacy runners without structured results fail at the adapter boundary.
6. SQLite repository connections now commit and close explicitly; PWA tests pass with `ResourceWarning` treated as an error.

## Safe Next Slice

Migrate the structured single-source provider result contract: make every adapter execution return an explicit `DownloadResult` or failure, require a real output path before success, and preserve playlist/CSV/loop compatibility entry points unchanged. Add tests for adapter success, adapter failure, missing result, missing path, and public API compatibility before deleting any legacy helper.

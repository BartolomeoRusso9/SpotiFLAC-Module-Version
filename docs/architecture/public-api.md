# Public API

This document defines the supported Python API surface for the 4.0.0 refactor.
Implementation modules under `SpotiFLAC.core`, `SpotiFLAC.application`, and
`SpotiFLAC.downloader` remain internal unless explicitly listed here.

## Clients

### `SpotiFLAC`

Synchronous compatibility wrapper for downloading one URL, a list of URLs, or
configured collections.

The existing positional and keyword arguments remain supported, including:

- output directory and filename settings;
- provider services and quality;
- fallback and retry settings;
- lyrics, Canvas, metadata enrichment, and transcoding options;
- resume and concurrency options.

### `AsyncSpotiFLAC`

Asynchronous client with the existing methods:

- `download_track(url, loop_minutes=None)`
- `download_batch(urls, loop_minutes=None)`
- `download_tracks(urls, loop_minutes=None, prefetched=None)`
- `get_playlist(url)`
- `get_track_metadata(url_or_id)`
- `search(query, limit=20)`

The additive application-layer entry point is:

```python
report = await client.download_request(request)
```

where `request` is a `DownloadRequest`. Existing client methods retain their
legacy return values and behavior during the incremental migration.

## Application Contracts

The following contracts are public and re-exported from `SpotiFLAC`:

- `SpotiFLACConfig`
- `DownloadRequest`
- `DownloadReport`
- `DownloadFailure`
- `DownloadSkip`
- `DownloadResult`
- `TrackMetadata`

`DownloadReport` is the common result contract for the application download
service. Its convenience properties include `total` and `success_count`.

## Compatibility Rules

- Existing client constructors and wrapper signatures remain supported.
- New application services are additive and do not replace legacy methods in a
  single release.
- Breaking changes require a new major API version or a documented deprecation
  period.
- REST consumers should use `/api/v1` and its declared Pydantic schemas.

## Internal Implementation Details

The following are not public API contracts:

- private helpers on `SpotiflacDownloader`;
- `launcher.py` helpers;
- modules under `SpotiFLAC.core` not re-exported by `SpotiFLAC`;
- application service internals and repository implementation details;
- GUI, TUI, and WebSocket transport helpers.

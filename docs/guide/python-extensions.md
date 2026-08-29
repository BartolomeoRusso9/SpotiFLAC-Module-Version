<!-- Developer guide for building a Python download-provider extension.
     Linked from docs/guide/extensions.md ("Developing Extensions"). -->

[← Back to the README](../../README.md) · [← Extensions overview](extensions.md)

# Writing a Python Extension

SpotiFLAC ships **no built-in download providers**. Every source — Qobuz, Tidal,
Amazon, Deezer, SoundCloud, … — is a *Python extension*: a ZIP file
(`.sflx` / `.spotiflac-ext`) containing a manifest and one Python module that
subclasses `BaseProvider`. The module is imported **in-process** and its
`download_track_async()` is called for every track SpotiFLAC wants from that
service.

This guide walks the whole path: scaffold → implement against the `core`
toolbox → test → package → publish in a registry.

> **Reference extensions.** The existing providers in
> `SpotiFLAC-Python-Extensions/extensions/*.sflx` are the canonical examples.
> `soundcloud_native.sflx` is the most readable end-to-end (search → resolve →
> stream → tag). Unzip one and read along.

---

## 1. How an extension is wired in

```
services=["ext:mysource-py"]         SpotiFLAC(...) / --service
        │
        ▼
_build_providers_for_name()          downloader.py
        │   resolves the name, honours "-py" (Python only) / "-web" (JS only)
        ▼
PythonExtensionProvider(ext_id)      extensions/python_provider.py
        │   finds the installed ext, imports the module,
        │   asserts EXACTLY ONE BaseProvider subclass, instantiates it
        ▼
YourProvider.download_track_async(metadata, output_dir, ...)  ← your code
        │   returns DownloadResult
        ▼
downloader applies tags / validation / dedup / stats
```

Key consequences:

- Your module **must expose exactly one** `BaseProvider` subclass. Two (or
  zero) is a hard load error. Import helper classes from `SpotiFLAC.core.*`,
  don't define extra `BaseProvider` subclasses in the same file.
- `metadata` already carries everything SpotiFLAC knows from Spotify (ISRC,
  title, artists, duration, ISRC, cover URL…). Your job is to **match** that
  track on your service and **download** it.
- Return a `DownloadResult` — never raise for "not found on my service";
  raise `TrackNotFoundError` so the downloader moves to the next provider.

---

## 2. Prerequisites

- SpotiFLAC installed in the environment you'll test with (`pip install -e .`
  from a checkout is fine — you need `SpotiFLAC.core` importable).
- Python 3.11+ (matches the project).
- Your extension may depend on third-party packages, but there is **no
  dependency install step** for `.sflx` packages — the module is imported as-is.
  Anything you `import` must already be present in the host environment. Stick
  to what SpotiFLAC already depends on (`httpx`, `pydantic`, `mutagen`,
  `aiofiles`, `cryptography`, `beautifulsoup4`) unless you also document the
  extra install for your users.

---

## 3. Scaffold

```bash
spotiflac --ext-scaffold mysource --runtime python
# writes ./mysource/{manifest.json, mysource.py, README.md}
```

`mysource.py` comes pre-filled with the correct `download_track_async`
signature and TODO markers. The generated manifest:

```json
{
  "name": "mysource",
  "displayName": "mysource",
  "version": "0.1.0",
  "description": "TODO: describe what mysource does.",
  "runtime": "python",
  "entryPoint": "mysource.py",
  "type": ["download_provider"],
  "urlHandler": { "patterns": [] },
  "settings": []
}
```

---

## 4. `manifest.json` reference

| Field | Required | Notes |
| --- | --- | --- |
| `name` | ✅ | `[A-Za-z0-9][A-Za-z0-9._-]*`. Decides the install directory and the `ext:<name>` id. Convention: the bare service name, e.g. `qobuz`. |
| `runtime` | ✅ | Must be `"python"`. |
| `entryPoint` | ✅ | The `.py` file at the ZIP root, e.g. `mysource.py`. |
| `version` | ✅ (dry-run) | SemVer. The registry compares this to decide "update available". |
| `type` | ✅ | `["download_provider"]` for a normal source. `["runtime_utility"]` for a shared helper (no `download_track_async`, not offered as a service). `["metadata_provider"]` is also recognized. |
| `displayName` | — | Shown in GUI lists. |
| `description` | — | Shown in GUI / registry. |
| `urlHandler.patterns` | — | Regexes for URLs your service owns (e.g. `soundcloud\.com`). Lets a user paste a native link instead of a Spotify one. |
| `settings` | — | `[{ "key": "...", "label": "...", "type": "string|password|bool", "default": ... }]`. Declares user-configurable values for the GUI. **Note:** the current download path (`_build_providers_for_name` → `PythonExtensionProvider`) does *not* forward these to your constructor — read credentials from environment variables instead (see §5). The field is still useful as documentation and for JS parity. |

**Legacy packages.** A ZIP with a single top-level `.py` and *no* manifest is
accepted as a "legacy python" package — the manifest is synthesized
(`version` `0.0.0+legacy`, `name` derived by stripping `_native`). The current
`.sflx` files in the extensions repo are legacy-style. Prefer a real manifest
for anything new.

---

## 5. The `BaseProvider` contract

Full source: `SpotiFLAC/core/base.py`. You implement **one** method.

```python
from __future__ import annotations

from collections.abc import Callable

from SpotiFLAC.core.base import BaseProvider
from SpotiFLAC.core.errors import TrackNotFoundError
from SpotiFLAC.core.models import DownloadResult, TrackMetadata


class MysourceProvider(BaseProvider):
    name = "mysource"  # short, lowercase; used in logs, {platform}, stats

    async def download_track_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format: str | Callable[..., str] = "{title} - {artist}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        artist_separator: str | None = None,
        allow_fallback: bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
        quality: str = "LOSSLESS",
        **kwargs,
    ) -> DownloadResult:
        ...
```

Always keep `**kwargs` — the downloader passes extra keywords over time
(`qobuz_token`, etc.) and you must not break on them.

### What `BaseProvider.__init__` gives you for free

| Attribute / method | Purpose |
| --- | --- |
| `self._async_http` | An `AsyncHttpClient` (see §7). Rate-limited, retrying, shared connection pool. |
| `self._progress_cb` | Callback `(written_bytes, total_bytes)` — pass it straight to `stream_to_file`. `None` when nobody is listening. |
| `self._build_output_path(...)` | Builds the final on-disk `Path`, applying `filename_format`, subfolders, the `{platform}`/`{id}` placeholders, and `mkdir -p`. |
| `self._file_exists(path)` | `True` if a valid file is already there (validates FLAC integrity, deletes corrupt files so they re-download). |
| `self._run_ffmpeg(*args)` / `self._run_ffprobe(*args)` | `async` subprocess wrappers → `(returncode, stdout, stderr)`. |
| `self.set_progress_callback` / `self.set_stop_event` | Called by the framework; you normally don't. |

If you override `__init__`, call `super().__init__(timeout_s=..., headers=...,
retry=..., rate_limiter=...)` so `self._async_http` is set up.

### Constructor & configuration

`PythonExtensionProvider` instantiates your class with **no arguments** in the
normal download path — `candidates[0](**kwargs)` where `kwargs` is empty. So:

- Either don't define `__init__` at all (use `BaseProvider`'s), or
- Define one where **every parameter has a default**, and keep `**kwargs`:

```python
class MysourceProvider(BaseProvider):
    def __init__(self, settings: dict | None = None, **kwargs) -> None:
        super().__init__(timeout_s=120)
        settings = settings or {}
        # settings is NOT populated by the current download path — read
        # credentials from the environment as the primary source:
        self._api_key = settings.get("api_key") or os.environ.get("MYSOURCE_TOKEN", "")
```

For env vars that the JS sandbox would strip, users add them to
`SPOTIFLAC_EXT_ENV_PASSTHROUGH` — but Python extensions share the host
environment directly, so any `os.environ` var works as-is.

---

## 6. `TrackMetadata` — what you receive

Source: `SpotiFLAC/core/models.py`. Pydantic model; the fields you'll actually
use to match a track:

| Field | Notes |
| --- | --- |
| `id` | The **Spotify** track id (not yours). |
| `isrc` | Often present — the best matching key. May be `""`. |
| `title`, `artists`, `album`, `album_artist` | `artists` is a `", "`-joined string; `feat.`/`&`/`/` already normalized to commas. |
| `duration_ms` | Use for match disambiguation (±a few seconds). |
| `track_number`, `disc_number`, `total_tracks`, `total_discs` | |
| `release_date` (`YYYY-MM-DD`), `.year` property | |
| `cover_url` | Spotify cover; pass to the tagger unless your service has a better one. |
| `external_url` | The public URL (Spotify, or a native URL if the user pasted one). |
| `upc`, `album_id`, `is_explicit`, `label`/`publisher`, `genre` | Frequently empty from Spotify. |
| `extra_info: dict` | Free-form. When a user pastes a native URL, providers stash hints here (e.g. `extra_info["provider"] == "soundcloud"`). |

Helpers: `metadata.first_artist`, `metadata.duration_seconds`,
`metadata.as_flac_tags()`.

---

## 7. The `core` toolbox

Everything under `SpotiFLAC.core.*` is importable from your extension. The ones
that matter for a download provider:

### `core.http` — `AsyncHttpClient`

Use `self._async_http`; don't create your own `httpx` client.

```python
resp = await self._async_http.get(url, params={...}, headers={...}, timeout=15.0)
data = await self._async_http.get_json_async(url)         # raises ParseError on bad JSON
resp = await self._async_http.post(url, json={...})

await self._async_http.stream_to_file(
    stream_url, str(dest_path), self._progress_cb,
    chunk_size=256 * 1024, resume=True,       # resumes from a leftover .part file
)
```

Status handling is automatic and typed: `401/403 → AuthError`, `404 →
TrackNotFoundError`, `429 → RateLimitedError` (honours `Retry-After`), other
5xx/4xx → `NetworkError`. `RateLimitedError` and `NetworkError` are retried
(3 attempts, exponential backoff) before propagating. Pass a
`RetryConfig(max_attempts=..., base_delay_s=...)` to `super().__init__` to tune.

Rate-limit yourself with `AsyncRateLimiter(max_requests, window_seconds)` passed
to `super().__init__(rate_limiter=...)`.

### `core.errors` — typed exceptions

| Raise | When |
| --- | --- |
| `TrackNotFoundError(self.name, "<artist> - <title>")` | Your service has no match. **Not fatal** — downloader tries the next provider. |
| `AuthError(self.name, msg)` | Bad/expired credentials. |
| `RateLimitedError(self.name, retry_after=30)` | You detected a soft-limit the HTTP layer didn't. |
| `ParseError`, `NetworkError`, `InvalidUrlError` | As named. |

All derive from `SpotiflacError` (`.kind: ErrorKind`, `.is_retryable()`).

### `core.models` — `DownloadResult`

Return one of:

```python
return DownloadResult.ok(self.name, str(path), fmt="flac")      # fmt: "flac"|"mp3"|"m4a"
return DownloadResult.skipped_result(self.name, str(path), fmt="flac")  # already on disk
return DownloadResult.fail(self.name, "human-readable reason")   # tried, failed
```

`build_filename(metadata, fmt, ...)` is the standalone version of
`_build_output_path`'s naming, if you need just the string.

### `core.tagger` — `embed_metadata_async` + `EmbedOptions`

After the file is written, tag it:

```python
from SpotiFLAC.core.tagger import EmbedOptions, embed_metadata_async

opts = EmbedOptions(
    first_artist_only=first_artist_only,
    cover_url=metadata.cover_url,
    embed_lyrics=embed_lyrics,
    lyrics_providers=[p for p in (lyrics_providers or []) if p != "spotify"],
    enrich=enrich_metadata,
    enrich_providers=enrich_providers,
    is_album=is_album,
    extra_tags={},                       # anything your service knows that Spotify didn't
)
try:
    await embed_metadata_async(str(dest), metadata, opts)
except Exception as exc:
    logger.warning("[mysource] tagging failed, file kept untagged: %s", exc)
```

Handles FLAC/MP3/M4A, cover download+embed, optional lyrics and MusicBrainz
enrichment. Always wrap it so a tagging failure doesn't lose the download.

### `core.download_validation` / `core.flac_validation`

```python
from SpotiFLAC.core.download_validation import validate_downloaded_track_async

ok, why = await validate_downloaded_track_async(str(dest), metadata.duration_ms // 1000)
if not ok:
    return DownloadResult.fail(self.name, why)   # e.g. caught a 30s preview
```

`validate_flac_file(path) -> (bool, str)` checks stream integrity (the
`_file_exists` helper already calls it for you).

### Other useful modules

| Module | Use |
| --- | --- |
| `core.isrc_utils` | `normalize_isrc(s)`, `is_valid_isrc(s)`, `confirm_isrc_with_qobuz_async(...)`. |
| `core.link_resolver` | `LinkResolver(AsyncHttpClient("odesli")).resolve_all_async(spotify_id)` → `{ "soundcloud": url, "tidal": url, ... }` cross-service links via Odesli. |
| `core.musicbrainz` | `fetch_mb_metadata_async(isrc)`, `mb_result_to_tags(...)` for richer tags. |
| `core.quality` | `normalize_quality(q)`, `quality_for_provider(name, q)`, `quality_fallback_chain(q)`. |
| `core.provider_stats` | `record_success_async(name)`, `record_failure_async(name)`, `prioritize_providers_async(...)` — feeds the adaptive provider ordering. |
| `core.console` | `print_source_banner(...)`, `print_api_failure(...)` for consistent CLI output. |

---

## 8. A complete minimal provider

```python
"""mysource — SpotiFLAC Python extension."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

from SpotiFLAC.core.base import BaseProvider
from SpotiFLAC.core.errors import TrackNotFoundError
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.core.tagger import EmbedOptions, embed_metadata_async
from SpotiFLAC.core.download_validation import validate_downloaded_track_async

logger = logging.getLogger(__name__)


class MysourceProvider(BaseProvider):
    name = "mysource"

    def __init__(self, settings: dict | None = None, **kwargs) -> None:
        super().__init__(timeout_s=120)
        self._token = (settings or {}).get("api_token", "") or os.environ.get("MYSOURCE_TOKEN", "")

    async def _find_track(self, metadata: TrackMetadata) -> dict | None:
        # 1. Try ISRC (exact), 2. fall back to text search + duration check.
        if metadata.isrc:
            data = await self._async_http.get_json_async(
                "https://api.mysource.example/v1/tracks",
                params={"isrc": metadata.isrc}, headers=self._auth(),
            )
            if data.get("items"):
                return data["items"][0]

        data = await self._async_http.get_json_async(
            "https://api.mysource.example/v1/search",
            params={"q": f"{metadata.title} {metadata.first_artist}", "type": "track"},
            headers=self._auth(),
        )
        for item in data.get("items", []):
            if abs(item.get("duration_ms", 0) - metadata.duration_ms) <= 3000:
                return item
        return None

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def download_track_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format: str | Callable[..., str] = "{title} - {artist}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        allow_fallback: bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
        quality: str = "LOSSLESS",
        **kwargs,
    ) -> DownloadResult:
        track = await self._find_track(metadata)
        if not track:
            raise TrackNotFoundError(self.name, f"{metadata.first_artist} - {metadata.title}")

        stream = await self._async_http.get_json_async(
            f"https://api.mysource.example/v1/tracks/{track['id']}/stream",
            params={"quality": "lossless"}, headers=self._auth(),
        )
        stream_url = stream["url"]
        ext = ".flac" if stream.get("format") == "flac" else ".mp3"

        dest = self._build_output_path(
            metadata, output_dir, filename_format, position,
            include_track_num, use_album_track_num, first_artist_only,
            extension=ext, native_id=str(track["id"]),
        )
        if self._file_exists(dest):
            return DownloadResult.skipped_result(self.name, str(dest), fmt=ext.lstrip("."))

        try:
            await self._async_http.stream_to_file(stream_url, str(dest), self._progress_cb)
        except Exception as exc:
            logger.exception("[mysource] download failed")
            if dest.exists():
                await asyncio.to_thread(dest.unlink, missing_ok=True)
            return DownloadResult.fail(self.name, str(exc))

        ok, why = await validate_downloaded_track_async(str(dest), metadata.duration_ms // 1000)
        if not ok:
            return DownloadResult.fail(self.name, why)

        try:
            await embed_metadata_async(
                str(dest), metadata,
                EmbedOptions(
                    first_artist_only=first_artist_only,
                    cover_url=metadata.cover_url,
                    embed_lyrics=embed_lyrics,
                    lyrics_providers=[p for p in (lyrics_providers or []) if p != "spotify"],
                    enrich=enrich_metadata,
                    enrich_providers=enrich_providers,
                    is_album=is_album,
                ),
            )
        except Exception as exc:
            logger.warning("[mysource] tagging failed: %s", exc)

        return DownloadResult.ok(self.name, str(dest), fmt=ext.lstrip("."))
```

### Handling pasted native URLs (optional)

If your manifest sets `urlHandler.patterns`, a user can paste
`https://mysource.example/track/123` directly. The downloader routes it to your
provider with hints in `metadata.extra_info` (and `metadata.external_url` set to
that URL). Check for it at the top of `download_track_async` and skip the
search step:

```python
if "mysource.example" in (metadata.external_url or ""):
    track_id = metadata.external_url.rstrip("/").split("/")[-1]
    ...
```

---

## 9. Progress & cancellation

- **Progress:** just forward `self._progress_cb` to `stream_to_file`. If you
  assemble the file yourself, call `self._progress_cb(written, total)` after
  each chunk (guard for `None`).
- **Cancellation:** the framework calls `set_stop_event(threading.Event)`; the
  same event is wired into `self._async_http`, so an in-flight `stream_to_file`
  aborts cleanly. For long non-HTTP loops, check
  `getattr(self, "_stop_event", None)` and bail.

---

## 10. Test locally

**Dry-run** (no install, no network, no registry):

```bash
spotiflac --ext-dry-run ./mysource
# checks: manifest parses, required fields, entry point imports,
#         exactly one BaseProvider subclass
```

**Install into your real extensions dir and run a download:**

```python
from SpotiFLAC.extensions import ExtensionManager
ExtensionManager(auto_install_downloads=False).install_from_file("./mysource.sflx")
```

```bash
spotiflac "https://open.spotify.com/track/..." ./out --service ext:mysource-py
```

The `-py` suffix forces the Python provider only (no JS fallback pairing).
Plain `--service mysource` also works once installed (legacy-alias resolution).

**Iterating:** re-`install_from_file` after each change, or point
`ExtensionManager(ext_dir=...)` at a scratch dir and pass
`SpotiFLAC(..., ext_dir=...)`.

---

## 11. Package as `.sflx`

A `.sflx` is a plain ZIP with **`manifest.json` at the root** (no wrapping
folder):

```bash
cd mysource
zip -r ../mysource.sflx manifest.json mysource.py
# keep only .py / .json; strip __MACOSX, .DS_Store
```

The extensions repo has `scripts/update_registry_and_folders.py` which converts
`.zip` → `.sflx`, flattens an accidental top-level folder, strips junk files,
computes SHA-256, and rewrites `registry.json`. Use it if you maintain a
registry.

Multi-file extensions are fine (put helpers next to the entry point); intra-
package imports resolve under `SpotiFLAC.extensions_plugins.<name>` once
`preload_python_modules()` runs, so `from .helper import X` works.

---

## 12. Publish in a registry

A registry is a JSON file you host anywhere. Entry shape:

```json
{
  "version": 1,
  "updated_at": "2026-08-29T00:00:00Z",
  "extensions": [
    {
      "id": "mysource-py",
      "name": "mysource-py",
      "display_name": "MySource",
      "version": "0.1.0",
      "description": "Lossless downloads from MySource.",
      "download_url": "https://example.com/mysource.sflx",
      "sha256": "<hex sha256 of the .sflx file>",
      "category": "download",
      "tags": ["mysource", "python"],
      "updated_at": "2026-08-29T00:00:00Z"
    }
  ]
}
```

- `sha256` is verified on install; a mismatch aborts (registry installs) unless
  `SPOTIFLAC_ALLOW_CHECKSUM_MISMATCH=1` (local file installs only). Omitting it
  installs "unverified".
- `category` in `{"download", "download_provider", "utility",
  "runtime_utility"}` (or a matching `tags` entry) makes it auto-install on
  startup when the registry is configured.
- Users add your registry with
  `spotiflac --registries https://example.com/registry.json` (persisted) or
  `SPOTIFLAC_REGISTRIES=...`.

**Optional Ed25519 signature** (lets users pin trust to your key):

```bash
python -m SpotiFLAC.tools.registry_signing_cli keygen
python -m SpotiFLAC.tools.registry_signing_cli sign \
  --private-key <base64> --id mysource-py --version 0.1.0 \
  --sha256 <hex> --download-url https://example.com/mysource.sflx
# paste the printed "signature": "..." into the entry
```

Canonical signed message is `id|version|sha256|download_url`. Users then run
`spotiflac --trust-key-add "you" "<public key>"` and optionally
`--min-trust-tier signed`.

---

## 13. Gotchas

- **No sandbox.** Python extensions are imported into the SpotiFLAC process —
  full access to its memory, env, filesystem. Only ship code you'd run
  yourself. (JS extensions get a limited env; Python gets none.)
- **Exactly one `BaseProvider` subclass** per module — enforced at load time
  and by `--ext-dry-run`.
- **`name` collisions.** Two installed extensions matching the same base name
  (`mysource` from `mysource-py` and `mysource-web`) are paired
  Python-first, JS-fallback. Pick a distinctive `name`.
- **Don't block the event loop.** Everything is `async`. Wrap blocking I/O in
  `asyncio.to_thread(...)`.
- **Keep `**kwargs`** in `download_track_async` and `__init__`.
- **Return, don't raise, for "no match"** — `TrackNotFoundError` specifically,
  so the fallback chain continues.
- **Dependencies aren't installed for you.** Only import what the host already
  has, or document the extra `pip install` for your users.

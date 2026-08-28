<!-- Extracted verbatim from README.md. The README had grown to 76 KB
     and 87 headings, which is past the point where either GitHub or
     PyPI renders it usefully. Nothing here was reworded in the split. -->

[← Back to the README](../../README.md)

# API Reference

## API Reference

### `SpotiFLAC()` Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | `str` / `list[str]` | Required | A single URL or a list of URLs (batch mode). |
| `output_dir` | `str` | Required | The destination directory path where the audio files will be saved. |
| `output_path` | `str` | `None` | Exact destination file path for single track downloads. Overrides `output_dir` + `filename_format`. Automatically ignored for albums, playlists and artist discographies. |
| `services` | `list` | `["ext:tidal-web"]` | Extensions to use and their priority order, as `ext:<id>` (or a legacy alias — see [Extensions](extensions.md#extensions)). Each `id` must correspond to an extension you have already installed; nothing is bundled or installed automatically. |
| `registries` | `list` | `None` | One or more extension-registry JSON URLs to add before the run, as an alternative to `SPOTIFLAC_REGISTRIES` or a `.env` file. Must be `https://`. Persisted to `~/.spotiflac/registry_settings.json` on first use, so subsequent runs (CLI, GUI, or Python) pick it up automatically without passing it again — see [Extensions](extensions.md#extensions). |
| `filename_format` | `str` | `"{title} - {artist}"` | Format for naming downloaded files. See placeholders below. |
| `use_track_numbers` | `bool` | `False` | Prefixes the filename with the track number. |
| `use_album_track_numbers` | `bool` | `False` | Uses the track's original album number instead of the download queue position. |
| `use_artist_subfolders` | `bool` | `False` | Automatically organizes downloaded files into subfolders by artist. |
| `use_album_subfolders` | `bool` | `False` | Automatically organizes downloaded files into subfolders by album. |
| `create_playlist_subfolders` | `bool` | `False` | Creates a subfolder per playlist/album when downloading a collection, in addition to any artist/album subfolders. |
| `first_artist_only` | `bool` | `False` | Uses only the first artist in tags and filename. |
| `artist_separator` | `str` | `None` | Custom separator (e.g. `", "` or `" / "`) to join multiple artists into a single string in tags, instead of using standard multi-value fields. Useful for players like Rekordbox. |
| `include_featuring` | `bool` | `False` | When downloading an artist discography, also includes tracks where the artist appears as a featured artist. |
| `max_concurrent_downloads` | `int` | `2` | How many tracks to download in parallel. |
| `tidal_custom_api` | `str` | `None` | Optional setting forwarded to the installed `tidal-web`-family extension, if it supports it. Has no effect on its own — see [Passing Settings to an Extension](configuration.md#passing-settings-to-an-extension-eg-a-self-hosted-api-instance). |
| `timeout_s` | `int` | `None` | Per-track download timeout in seconds. If a single track download does not complete within this time, the process is terminated and the track is marked as failed. SpotiFLAC then moves on to the next extension or retry. Set to `None` (default) to disable the timeout. |
| `loop` | `int` | `None` | Duration in minutes to keep retrying permanently failed tracks after a full session completes. |
| `track_max_retries` | `int` | `0` | Extra download attempts per track when all extensions fail on the first try. Each retry cycles through all configured extensions again with exponential backoff (2 s → 4 s → 8 s …, capped at 30 s). |
| `quality` | `str` | `"LOSSLESS"` | Requested profile: `LOSSLESS` or `HI_RES_LOSSLESS`. `DOLBY_ATMOS` is also accepted but is Tidal-exclusive — any other provider falls back to `HI_RES_LOSSLESS` instead. Legacy provider-specific values are accepted and normalized. |
| `allow_fallback` | `bool` | `True` | For `HI_RES_LOSSLESS`, allows fallback to `LOSSLESS` when the higher-resolution tier is unavailable. It never downgrades lossless requests to compressed audio. |
| `log_level` | `int` | `logging.WARNING` | Python logging level. |
| `embed_lyrics` | `bool` | `True` | Whether to fetch and embed synchronized lyrics (LRC) into the audio file. |
| `lyrics_providers` | `list` | `["spotify", "apple", "musixmatch", "lrclib", "amazon"]` | Priority order of lyrics providers to attempt. |
| `enrich_metadata` | `bool` | `True` | Enables multi-provider metadata enrichment (HD covers, BPM, labels, etc.). |
| `enrich_providers` | `list` | `["deezer", "apple", "qobuz", "tidal"]` | Priority order of metadata providers to attempt. `soundcloud` is also accepted but isn't on by default. |
| `qobuz_token` | `str` | `None` | Optional setting forwarded to the installed Qobuz extension, if it supports it. Has no built-in behavior of its own. |
| `qobuz_local_api_url` | `str` | `None` | Optional setting forwarded to the installed `qobuz-web`-family extension, if it supports it. Has no effect on its own — see [Passing Settings to an Extension](configuration.md#passing-settings-to-an-extension-eg-a-self-hosted-api-instance). |
| `use_extensions_fallback` | `bool` | `True` | Whether to automatically fall back to another installed extension for the same alias if one fails. Set to `False` to use only the extensions explicitly listed in `services`. |
| `transcode_to` | `str` | `None` | Converts every finished track to this format. Currently only `"mp3"` (see [MP3 Transcoding](configuration.md#mp3-transcoding)). `None` keeps the extension's original format. Requires `ffmpeg`. |
| `transcode_bitrate` | `str` | `"320k"` | Bitrate used by `transcode_to`, e.g. `"320k"`, `"256k"`, `"192k"`. |
| `transcode_keep_original` | `bool` | `False` | Keeps the original lossless file next to the converted one. By default the source is deleted once the conversion succeeds. |
| `verify_hires` | `bool` | `False` | Runs a spectral-analysis QA check after each successful lossless download, flagging files that declare a high sample rate but lack real content above standard-definition frequencies (possible upsampling / fake Hi-Res). Requires the optional `librosa`/`numpy` packages (`pip install SpotiFLAC[hires]`); silently skipped if they're not installed. Never fails or delays a download — see [Hi-Res Verification](configuration.md#hi-res-verification). |
| `post_download_action` | `str` | `"none"` | Action after all downloads finish: `"none"`, `"open_folder"`, `"notify"`, `"command"`. |
| `post_download_command` | `str` | `""` | Shell command to run when `post_download_action="command"`. Supports `{folder}`, `{succeeded}`, `{skipped}`, `{failed}` placeholders; quote `{folder}` in your template (e.g. `'{folder}'`) since the substituted path may contain spaces. |

### Filename Format Placeholders & Custom Formatting

#### String Template with Placeholders

When customizing the `filename_format` string, you can use the following dynamic tags:

- `{title}` — Track title
- `{artist}` — Track artist(s)
- `{album}` — Album name
- `{album_artist}` — The artist(s) of the entire album
- `{disc}` — The disc number
- `{track}` — The track's original number in the album
- `{position}` — Download queue / playlist position (zero-padded, e.g. `01`)
- `{date}` — Full release date (e.g., `YYYY-MM-DD`)
- `{year}` — Release year (e.g., `YYYY`)
- `{isrc}` — Track ISRC code
- `{platform}` — Source platform (e.g., `"tidal"`, `"soundcloud"`, `"youtube"`) — *the extension/service that provided the download*
- `{id}` — Platform-specific track ID (e.g., Tidal track ID, SoundCloud user ID, YouTube video ID) — useful for tracing a file back to its origin

**Examples:**

```python
SpotiFLAC(
    url="https://open.spotify.com/track/...",
    output_dir="./downloads",
    services=["ext:tidal-web"],
    # Standard string template
    filename_format="{year} - {album}/{track}. {title}",
)

# Using platform and ID in the filename (flat):
SpotiFLAC(
    url="https://open.spotify.com/playlist/...",
    output_dir="./downloads",
    filename_format="{platform}_{album}_{title}",
)
```

#### Custom Function (Lambda) for Advanced Logic

For complex naming rules, pass a **callable** (function or lambda) instead of a string. The function receives:

- `metadata` — the `TrackMetadata` object
- `platform` — the source platform string (e.g., `"tidal"`)
- `native_id` — the platform-specific ID, or `None` if not available
- `**kwargs` — additional context

The function must return a **filename** (without directory or extension — those are added automatically).

**Example — use ISRC if available, fall back to platform_id:**

```python
from SpotiFLAC import SpotiFLAC

def my_filename_logic(metadata, platform, native_id, **kwargs):
    """
    Prioritize ISRC, then fall back to platform_id for traceability,
    then just use the title.
    """
    if metadata.isrc:
        return metadata.isrc
    
    if native_id:
        return f"{platform}_{native_id}"
    
    return metadata.title

SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["ext:tidal-web", "ext:soundcloud-web"],
    filename_format=my_filename_logic,  # Pass the function directly
)
```

**Example — include platform and year in filename (flat):**

```python
SpotiFLAC(
    url="https://open.spotify.com/playlist/...",
    output_dir="./downloads",
    filename_format=lambda metadata, platform, native_id, **kw: (
        f"{platform}_{metadata.year or 'unknown'}_{metadata.title}"
    ),
)
```

### CLI Flag Reference

| Flag | Short | Default | Description |
| --- | --- | --- | --- |
| `--service` | `-s` | `ext:tidal-web` | One or more extensions in priority order, as `ext:<id>` (or a legacy alias resolved to an installed extension — see [Extensions](extensions.md#extensions)). |
| `--registries` | | `None` | An extension-registry JSON URL to add before running; repeat the flag for each one. Alternative to `SPOTIFLAC_REGISTRIES` or a `.env` file. Must be `https://`. Persisted to `~/.spotiflac/registry_settings.json`, so you only need to pass it once — see [Extensions](extensions.md#extensions). |
| `--filename-format` | `-f` | `{title} - {artist}` | Filename template with placeholders. |
| `--output-path` | `-o` | `None` | Exact output file path for single track downloads. Ignored for albums, playlists and discographies. |
| `--quality` | `-q` | `LOSSLESS` | Requested profile: `LOSSLESS` or `HI_RES_LOSSLESS`. `DOLBY_ATMOS` is also accepted but is Tidal-exclusive — any other provider falls back to `HI_RES_LOSSLESS` instead. Legacy provider-specific values are accepted and normalized. |
| `--use-track-numbers` | | `False` | Prefix filenames with track numbers. |
| `--use-album-track-numbers` | | `False` | Use the track's original album number instead of queue position. |
| `--use-artist-subfolders` | | `False` | Organize files into per-artist subfolders. |
| `--use-album-subfolders` | | `False` | Organize files into per-album subfolders. |
| `--playlist-subfolders` | | `True` | Create a subfolder for playlist downloads (enabled by default). |
| `--no-playlist-subfolders` | | | Keep playlist downloads directly in the output directory instead of a subfolder. |
| `--first-artist-only` | | `False` | Use only the first artist in tags and filename. |
| `--artist-separator` | | `None` | Custom separator for joining multiple artists in tags (e.g. `", "` or `" / "`). Useful for Rekordbox. |
| `--include-featuring` | | `False` | Include tracks where the artist appears as a featured artist. Only applies to artist/discography URLs. |
| `--qobuz-local-api` | | `None` | Optional setting forwarded to the installed Qobuz extension, if it supports it. |
| `--tidal-api` | | `None` | Optional setting forwarded to the installed Tidal extension, if it supports it. |
| `--timeout` | | `180` | Maximum seconds allowed for each provider attempt. If a track download stalls or takes longer than this limit, it is forcibly terminated and marked as failed, then SpotiFLAC moves to the next extension or retry. |
| `--loop` | `-l` | `None` | Keep retrying permanently failed tracks every N minutes. |
| `--watch` | | `None` | Re-run this exact command every N minutes, forever, instead of exiting after one pass. See [Watch Mode](configuration.md#watch-mode-keep-syncing-on-an-interval). |
| `--retries` | | `0` | Extra per-track download attempts on failure. Cycles through all configured extensions with exponential backoff. |
| `--max-concurrent` | | `2` | How many tracks to download at once. Each track still tries its providers in order/fallback on its own — this only controls how many tracks run simultaneously. Use `1` for fully sequential downloads with no interleaved console output. |
| `--playlist` | `-p` | `None` | Playlist URL to sync; repeat once per playlist. All tracks go to a single destination folder, shared tracks are downloaded once, and each playlist gets its own M3U file (see [Multiple Playlists in One Folder](configuration.md#multiple-playlists-in-one-folder)). |
| `--m3u` | | `m3u8` | Playlist file written for each `--playlist`: `m3u8`, `m3u` or `none`. Rewritten only when its content changed. |
| `--transcode` | | `none` | Convert every downloaded track to this format: `none` or `mp3`. Requires `ffmpeg`. |
| `--mp3` | | | Shorthand for `--transcode mp3`. |
| `--transcode-bitrate` | | `320k` | Bitrate used by `--transcode`, e.g. `320k`, `256k`, `192k`. |
| `--keep-original` | | `False` | Keep the original lossless file alongside the transcoded one. |
| `--verify-hires` | | `False` | Runs a spectral-analysis QA check after each successful lossless download, flagging files that declare a high sample rate but lack real content above standard-definition frequencies (possible upsampling / fake Hi-Res). Requires the optional `librosa`/`numpy` packages (`pip install SpotiFLAC[hires]`); silently skipped if they're not installed. Never fails or delays a download — see [Hi-Res Verification](configuration.md#hi-res-verification). |
| `--verbose` | `-v` | `False` | Enable debug logging. |
| `--no-lyrics` | | `False` | Disable lyrics embedding (lyrics are embedded by default). |
| `--lyrics-providers` | | `apple lrclib` | Lyrics provider priority order (CLI default; the Python API default is `spotify apple musixmatch lrclib amazon` when `lyrics_providers` is left unset). |
| `--no-enrich` | | `False` | Disable multi-provider metadata enrichment (enrichment is enabled by default). |
| `--enrich-providers` | | `deezer apple qobuz tidal` | Metadata enrichment provider priority order. `soundcloud` is also accepted but isn't on by default. |
| `--post-action` | | `none` | Action after all downloads finish: `none`, `open_folder`, `notify`, `command`. |
| `--post-command` | | `""` | Shell command for `--post-action=command`. Placeholders: `{folder}`, `{succeeded}`, `{skipped}`, `{failed}`; quote `{folder}` in your template (e.g. `'{folder}'`) since the substituted path may contain spaces. |
| `--profile` | | `None` | Load a saved profile. CLI flags override profile values. |
| `--save-profile` | | `None` | Save current CLI configuration as a named profile after the run. |
| `--gui` | | `False` | Launch the GUI as a native window (pywebview). See [GUI Mode](quick-start.md#gui-mode-recommended-for-most-users). |
| `--web` | | `False` | Launch the same GUI as a local web server instead of a native window. See [Web Mode](quick-start.md#web-mode-same-gui-in-your-browser). |
| `--host` | | `127.0.0.1` | Host to bind `--web` to. Only change this deliberately — see the security note under [Web Mode](quick-start.md#web-mode-same-gui-in-your-browser). |
| `--port` | | `8000` | Port to bind `--web` to. |
| `--web-token` | | `None` | Shared secret required on every `--web` request. Falls back to `SPOTIFLAC_WEB_TOKEN`. See [Authentication](quick-start.md#authentication---web-token). |
| `--web-multiuser` | | `False` | Require per-account login for `--web` instead of/alongside `--web-token`. See [Multi-user mode](quick-start.md#multi-user-mode---web-multiuser). |
| `--web-user-add` | | | Create a `--web-multiuser` account: `--web-user-add USERNAME PASSWORD`. |
| `--web-user-remove` | | | Delete a `--web-multiuser` account by username. |
| `--web-user-list` | | | List configured `--web-multiuser` usernames. |
| `--interactive` | | `False` | Launch the interactive step-by-step wizard. See [Interactive Mode](quick-start.md#interactive-mode-step-by-step-wizard). |
| `--registry-directories` | | `None` | A directory JSON URL to add before running (lists registries, not extensions — repeat once per URL). Alternative to `SPOTIFLAC_REGISTRY_DIRECTORIES`. See [Extension Discovery](extensions.md#extension-discovery-directories). |
| `--trust-key-add` | | | Trust a registry-signing public key: `--trust-key-add NAME PUBLIC_KEY_B64`. See [Registry Trust](extensions.md#registry-trust-signed-extensions). |
| `--trust-key-remove` | | | Remove a trusted key by name. |
| `--trust-key-list` | | | List trusted key names/public keys. |
| `--ext-scaffold` | | | Generate a new extension skeleton: `--ext-scaffold NAME [--runtime python\|javascript] [--output-dir DIR]`. See [Developing Extensions](extensions.md#developing-extensions). |
| `--ext-dry-run` | | | Validate an extension (directory or packaged ZIP) without installing it or contacting any registry: `--ext-dry-run PATH`. |

---

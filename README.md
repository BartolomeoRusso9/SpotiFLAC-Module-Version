<div align="left">
  <h1>SpotiFLAC Python Module</h1>
  <p>
    Fetch Spotify track metadata and retrieve matching lossless audio through Tidal, Qobuz, Amazon Music & other provider backends — supplied entirely via extensions you choose and configure yourself.
    Integrate directly into your Python projects, build custom Telegram bots, automation tools, or bulk downloaders.
  </p>

  <p>
    <a href="https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/stargazers"><img src="https://img.shields.io/github/stars/BartolomeoRusso9/SpotiFLAC-Module-Version?color=ffcb47&labelColor=black&logo=github&label=Stars" /></a>
    <a href="https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/releases/latest"><img src="https://img.shields.io/github/v/release/BartolomeoRusso9/SpotiFLAC-Module-Version?color=8b5cf6&labelColor=black&logo=github&label=Latest%20Release" /></a>
    <a href="https://pypi.org/project/SpotiFLAC/"><img src="https://img.shields.io/pypi/v/spotiflac?logo=pypi&logoColor=ffffff&labelColor=000000&color=7b97ed" /></a>
    <a href="https://pypi.org/project/SpotiFLAC/"><img src="https://img.shields.io/pypi/pyversions/spotiflac?logo=python&logoColor=ffffff&labelColor=000000&color=7b97ed" /></a>
    <a href="https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/releases"><img src="https://img.shields.io/github/downloads/BartolomeoRusso9/SpotiFLAC-Module-Version/total?color=22c55e&labelColor=black&logo=github&label=Downloads" /></a>
    <a href="https://pypi.org/project/SpotiFLAC/"><img src="https://img.shields.io/pepy/dt/spotiflac?logo=pypi&logoColor=ffffff&labelColor=000000" /></a>
    <a href="https://t.me/SpotiFLAC_Module_Version" target="_blank"><img src="https://img.shields.io/badge/Telegram%20Community-369eff?labelColor=black&logo=telegram&logoColor=white" /></a>
  </p>
</div>

## Disclaimer

This project is intended for **educational and personal use only**. The developer does not condone or encourage copyright infringement. The software is licensed under the [MIT License](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/blob/main/LICENSE).

**SpotiFLAC-Module-Version** is an independent, third-party tool and is not affiliated with, endorsed by, or connected to Spotify, Tidal, Qobuz, Amazon Music, Deezer, or any other streaming service. It is also not affiliated with, and has no control over or responsibility for, any other project sharing a similar name on other platforms.

No copyrighted content is hosted, stored, mirrored, or distributed by this repository. The core application does not bundle, ship, or default to any third-party extension, registry, or provider. Extensions are installed only if a user explicitly configures and requests them from a source of their own choosing; the maintainer has no control over, does not review, and assumes no responsibility for the content or behavior of third-party extensions or registries a user may choose to install.

You are solely responsible for:

1. Ensuring your use of this software, and any extension or registry you choose to install, complies with your local laws.
2. Reading and adhering to the Terms of Service of any platform or provider you access, directly or through an extension.
3. Any legal consequences resulting from the use or misuse of this tool.

This software is provided free of charge by the maintainer. If you paid a third party for access to it, you may have been misled or scammed.

The software is provided "as is", without warranty of any kind, express or implied. The author assumes no liability for any bans, damages, or legal issues arising from its use or misuse. Users assume all risk associated with its use.

If you are a copyright holder or an authorized representative and believe this repository infringes upon your rights, please contact the maintainer with sufficient detail (including relevant URLs and proof of ownership); the matter will be promptly investigated.

---

> **Looking for a standalone app?**
> - [SpotiFLAC (Desktop)](https://github.com/afkarxyz/SpotiFLAC) — Download music in true lossless FLAC from different providers for Windows, macOS & Linux
> - [SpotiFLAC (Mobile)](https://github.com/zarzet/SpotiFLAC-Mobile) — SpotiFLAC for Android & iOS, maintained by [@zarzet](https://github.com/zarzet)

---

## Features

- Native synchronous and asynchronous Python APIs
- Modular JavaScript Extension system (bring-your-own registry — nothing bundled)
- Automatic fallback among the extensions *you* have installed
- Built-in GUI
- Interactive CLI Wizard
- Docker support
- Configuration Profiles
- MusicBrainz metadata enrichment
- Embedded synchronized lyrics
- Optional MP3 320 kbps transcoding

---

## Installation

```bash
pip install SpotiFLAC
```

> **Important:** out of the box, SpotiFLAC does nothing but resolve Spotify metadata — it ships with **no built-in provider and no default extension source**. Before you can download anything, you need to point it at an extension registry of your own choosing and install at least one extension. See [JavaScript Extensions](#javascript-extensions) below.

---

## Quick Start

SpotiFLAC can be used in multiple ways. Choose the mode that fits your needs.

### GUI Mode (recommended for most users)

Launch the graphical user interface with the `--gui` flag:

```bash
spotiflac --gui
```

*(Or `python launcher.py --gui` if running from source)*

### Interactive Mode (step-by-step wizard)

SpotiFLAC features a smart Interactive Wizard that guides you step-by-step. To launch the wizard, use the `--interactive` flag:

```bash
spotiflac --interactive
```

*(Or `python launcher.py --interactive` if running from source)*

On launch it automatically runs a service health check before asking any questions, so you always know which of your installed extensions are reachable.

**What the wizard does at startup:**

- **Service Health Check** — probes the endpoints of your installed extensions and shows availability inline (✅ / ❌) before asking anything
- **URL History** — shows your last 8 downloads so you can re-run one with a single keypress
- **Folder Memory** — remembers your last output directory and offers it as the default
- **Profile Load** — optionally restores a full saved configuration

**Smart URL Detection:** If you input an Artist URL, it will ask if you want to download "Featuring" tracks. It skips this question for albums or playlists.

**Smart File Paths:** If you input a Single Track URL, it will ask if you want to set a specific `.flac` output path. If you do, it intelligently skips all questions about filename formatting and subfolder organization.

**Unified Quality Profiles:** Automatically translates your desired quality tier across different services.

**CLI Generator:** At the end of the configuration, it generates and prints the exact CLI command for your specific setup, so you can copy and reuse it in your automated scripts.

**Profile Save:** After confirming the download, you can save the entire configuration as a named profile to reuse later.

### Python API (Synchronous)

The classic synchronous API remains the simplest way to integrate SpotiFLAC into your own applications. `services` accepts either a legacy alias (resolved to `ext:<id>` if you have a matching extension installed) or an explicit `ext:<id>`.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/TRACK_ID",
    output_dir="./downloads",
    services=["ext:tidal-web"],  # requires the corresponding extension to already be installed
)
```

This API is fully backwards-compatible with previous releases and is recommended for scripts and applications that do not require asynchronous execution.

### Which API should I use?

| API | Best for |
|---|---|
| `SpotiFLAC` | Scripts, CLI wrappers, automation |
| `AsyncSpotiFLAC` | Discord bots, Telegram bots, FastAPI, asyncio applications |

### Asynchronous API

SpotiFLAC now features a 100% native asynchronous engine, making it ideal for modern Python applications built on asyncio, including:

- Discord bots
- Telegram bots
- FastAPI applications
- Quart / Sanic web servers
- Background workers
- Any asynchronous Python project

The new `AsyncSpotiFLAC` client uses a shared asynchronous HTTP session, allowing multiple downloads and metadata requests to run efficiently without blocking the event loop.

```python
import asyncio
from SpotiFLAC import AsyncSpotiFLAC

async def main():
    async with AsyncSpotiFLAC(
        output_dir="./downloads",
        services=["ext:tidal-web", "ext:qobuz-web"],
        quality="LOSSLESS",
    ) as client:

        # Download a single track
        await client.download_track(
            "https://open.spotify.com/track/TRACK_ID"
        )

        # Fetch playlist metadata without downloading
        info, tracks = await client.get_playlist(
            "https://open.spotify.com/playlist/PLAYLIST_ID"
        )

        print(f"{info['name']} contains {len(tracks)} tracks")

asyncio.run(main())
```

**Why use the async API?**

- Fully non-blocking (asyncio native)
- Shared HTTP connection pooling
- Lower memory usage
- Much better performance when downloading multiple tracks concurrently
- Perfect for long-running applications and web backends

> **Note:** The classic synchronous `SpotiFLAC()` API remains fully supported and backwards-compatible.

---

## JavaScript Extensions

SpotiFLAC has **no built-in download provider and no default extension source**. Every provider — Tidal, Qobuz, Amazon Music, Deezer, or anything else — is supplied entirely by extensions that you find, review, and choose to install yourself.

Extensions are never fetched or installed automatically. You must explicitly configure a registry before SpotiFLAC will contact anything:

```bash
# Comma-separated list of registry JSON URLs — none is set by default
export SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json"
```

or in a local `.env` file (see `.env.example`):

```
SPOTIFLAC_REGISTRIES=https://example.com/my-registry.json
```

Once configured, you can install and manage extensions:

```python
from SpotiFLAC.extensions import ExtensionManager

em = ExtensionManager()
em.install("some-extension-id", registry_url="https://example.com/my-registry.json")
```

Extensions use the `ext:` prefix and are referenced like any other provider:

```bash
spotiflac URL ./out \
  --service ext:tidal-web ext:qobuz-web
```

> **Note:** If Node.js is not installed, SpotiFLAC automatically attempts to install it the first time a JavaScript extension is used.
>
> Supported package managers:
> - **Linux:** apt-get, dnf, yum, pacman
> - **macOS:** brew
> - **Windows:** winget, choco

> **A note on legacy names:** for backwards compatibility, short names like `tidal`, `qobuz`, `amazon`, `deezer`, `apple`, `soundcloud`, `youtube`, `pandora` are still accepted in `services`/`--service`, and are resolved to an installed extension with a matching ID (e.g. `tidal` → `ext:tidal-web`) if — and only if — you have that extension installed. They are aliases, not built-in providers; nothing downloads without an extension behind it.
>
> The maintainer does not review, endorse, or take responsibility for the content or behavior of any third-party registry or extension. Choose your sources with the same care you would apply to installing any other third-party code.

---

## Docker Usage & Headless Automation

A lightweight, CLI-focused Docker image is available for running SpotiFLAC on servers, NAS devices, or any headless environment.

### Build the Image

```bash
docker build -t spotiflac .
```

### Basic Docker Usage

Run a download by mounting local directories to persist your downloads, configuration, cache, and extension registry across container restarts. Remember to also pass `SPOTIFLAC_REGISTRIES` (via `-e` or an `.env` file) since none is configured by default:

```bash
docker run --rm -it \
  -e SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json" \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/.spotiflac_docker:/root/.spotiflac" \
  -v "$(pwd)/.cache_docker:/root/.cache/spotiflac" \
  spotiflac "https://open.spotify.com/track/TRACK_ID" \
  /app/downloads -s ext:deezer-web -q LOSSLESS
```

### Published Image (GHCR)

Official Docker images are published on GitHub Container Registry (GHCR), allowing you to run the latest version without building locally.

```bash
docker pull ghcr.io/bartolomeorusso9/spotiflac-module-version:latest
```

### Logs in Headless Environments

A progress bar is a stream of carriage returns: readable on a terminal, unreadable in a log file. `docker logs` collapses each refresh into a `[285B blob data]` line, which buries everything worth reading.

SpotiFLAC therefore draws animated bars only when stderr is an interactive terminal. Everywhere else — Docker, cron, a redirected file — it prints the same information as plain lines instead:

```
[RUN] 24 track(s) · ext:tidal-web, ext:qobuz-web · LOSSLESS · 2 in parallel → /app/downloads
Track [3/24] Track Title — Artist Name (Album Name)
  ⬇  Track Title  ·  47%  ·  13.4 MB / 28.4 MB
  ✓  Track Title  ·  TIDAL-WEB  ·  FLAC  ·  28.4 MB  ·  12s
```

Progress lines are throttled to at most one per 25% and per 10 seconds, so a track costs a handful of lines rather than one per received chunk.

Set `SPOTIFLAC_PROGRESS_BARS` to override the detection in either direction:

```bash
export SPOTIFLAC_PROGRESS_BARS=0   # never draw bars, even on a terminal
export SPOTIFLAC_PROGRESS_BARS=1   # always draw bars
```

---

## Supported URL Types

SpotiFLAC's core resolves the following URL formats as input; whether a given target is actually reachable depends entirely on which extensions you have installed:

| Type | Spotify |
|---|---|
| Track | `open.spotify.com/track/...` |
| Album | `open.spotify.com/album/...` |
| Playlist | `open.spotify.com/playlist/...` |
| Discography (via artist URL) | `open.spotify.com/artist/...` |

> Extensions may add support for resolving Tidal, Apple Music, SoundCloud, YouTube, Pandora, or other platform URLs directly, and may output FLAC, ALAC/M4A, AAC, or MP3 depending on what the source and the extension support. Consult the documentation of the specific extension you install for its supported URL formats and output format.

---

## Advanced Configuration

You can customize the download behavior, prioritize specific installed extensions, and organize your files automatically into folders.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/ALBUM_ID",
    output_dir="./MusicLibrary",
    services=["ext:qobuz-web", "ext:amazon-web", "ext:tidal-web"],
    filename_format="{year} - {album}/{track}. {title}",
    use_artist_subfolders=True,
    use_album_subfolders=True,
    loop=60,                     # retry duration in minutes
    track_max_retries=2,         # extra per-track retries on failure
    post_download_action="notify"
)
```

### Service Health Check

SpotiFLAC can probe the endpoints of your installed extensions before downloading, to verify which ones are currently reachable.

In Interactive Mode this runs automatically at startup. In code or scripts you can call it directly:

```python
from SpotiFLAC.core.health_check import (
    run_health_check,
    print_health_report,
    get_working_providers,
)

results = run_health_check(["ext:tidal-web", "ext:qobuz-web", "ext:deezer-web"])
print_health_report(results)

working = get_working_providers(results)
print("Available providers:", working)
```

```bash
# CLI: check installed extensions then download
spotiflac https://open.spotify.com/track/... ./out --service ext:tidal-web ext:qobuz-web
```

The health check runs in parallel with a configurable timeout (default: 5 s per endpoint) and never blocks your download if a check fails. In the GUI, the check reports provider-level availability and endpoint counts, without exposing individual raw endpoint URLs.

### Configuration Profiles

Save and reuse complete download configurations without re-typing them every time.

**Save a profile**

```bash
# Save current flags as "hires-tidal"
spotiflac https://... ./out \
  --service ext:tidal-web \
  --quality HI_RES_LOSSLESS \
  --use-album-subfolders \
  --filename-format "{year} - {album}/{track}. {title}" \
  --save-profile hires-tidal
```

**Load a profile**

```bash
# Load "hires-tidal" — flags override profile values when both are present
spotiflac https://... ./out --profile hires-tidal
```

**In Python**

```python
import asyncio
from SpotiFLAC.core.profiles import (
    save_profile_async,
    get_profile_async,
    list_profiles_async,
)

async def main():
    await save_profile_async("hires-tidal", {
        "services":             ["ext:tidal-web"],
        "quality":              "HI_RES_LOSSLESS",
        "use_album_subfolders": True,
        "filename_format":      "{year} - {album}/{track}. {title}",
    })

    cfg = await get_profile_async("hires-tidal")
    print(await list_profiles_async())  # ['hires-tidal']

asyncio.run(main())
```

Profiles are stored at `~/.cache/spotiflac/profiles.json`. In the Interactive Wizard, you are prompted to load a profile at startup and optionally save one at the end.

### Batch Downloads

Pass a list of URLs to download them all in sequence. Failed tracks per URL are collected and can be retried with `loop`.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url=[
        "https://open.spotify.com/album/ALBUM_ID",
        "https://open.spotify.com/playlist/PLAYLIST_ID",
    ],
    output_dir="./MusicLibrary",
    services=["ext:tidal-web", "ext:qobuz-web"],
    use_album_subfolders=True,
)
```

### Auto-Retry on Failure

Set `track_max_retries` (Python) or `--retries` (CLI) to automatically retry failed tracks. Each retry cycles through all configured extensions from the beginning, waiting exponentially longer between attempts (2 s → 4 s → 8 s …, capped at 30 s).

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["ext:tidal-web", "ext:qobuz-web", "ext:deezer-web"],
    track_max_retries=3,   # up to 3 extra attempts per track
)
```

```bash
spotiflac https://open.spotify.com/album/... ./out \
  --service ext:tidal-web ext:qobuz-web ext:deezer-web \
  --retries 3
```

> **Tip:** Combine `--retries` with `--loop` for maximum resilience — `--retries` handles transient errors on individual tracks, while `--loop` re-queues permanently failed tracks after N minutes.

### Per-Track Timeout

Set `timeout_s` (Python) or `--timeout` (CLI) to cap the time SpotiFLAC will spend downloading a single track. If the download does not complete within the specified number of seconds, the process is terminated and the track is marked as failed — allowing the next extension or retry to take over.

```bash
# CLI — skip any track that takes more than 3 minutes
spotiflac https://open.spotify.com/album/... ./out --service ext:tidal-web --timeout 180
```

```python
# Python API
from SpotiFLAC import SpotiFLAC
SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["ext:tidal-web", "ext:qobuz-web"],
    timeout_s=120,
)
```

> **Tip:** Pair `--timeout` with `--retries` so that a stalled track is automatically re-attempted against the next extension instead of blocking the entire queue indefinitely.

### MP3 Transcoding

Downloads always fetch the best source an extension offers (FLAC, ALAC/M4A, …). Set `transcode_to="mp3"` (Python) or `--mp3` / `--transcode mp3` (CLI) to convert every finished track to MP3 — 320 kbps by default — for players or car stereos that cannot handle lossless files. Tags, cover art and lyrics are carried over to the MP3, and the original file is deleted once the conversion succeeds unless `transcode_keep_original` / `--keep-original` is set.

Requires `ffmpeg` on your `PATH`: the run stops immediately with a clear error if it is missing, so you never download a whole album only to fail at the conversion step.

```bash
# CLI — every track ends up as a 320 kbps MP3
spotiflac https://open.spotify.com/album/... ./out --service ext:tidal-web --mp3

# Keep the FLAC too, and use 192 kbps instead
spotiflac https://open.spotify.com/album/... ./out --mp3 --transcode-bitrate 192k --keep-original
```

```python
# Python API
from SpotiFLAC import SpotiFLAC
SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["ext:tidal-web", "ext:qobuz-web"],
    transcode_to="mp3",
    transcode_bitrate="320k",
)
```

**Skipping already-downloaded tracks still works.** The converted file keeps the exact name the extension would have used, only with an `.mp3` extension, so SpotiFLAC looks for that file *before* contacting any extension and skips the track when it is already there — no network request, no re-encode. Running the same album twice therefore costs nothing the second time. A leftover file from an earlier lossless run is converted in place instead of being re-downloaded, so an existing library converges to MP3 in a single pass.

The conversion is a no-op for extensions that already deliver MP3, which are passed through untouched.

### Multiple Playlists in One Folder

Pass `--playlist` (`-p`) once per playlist to sync several of them into a **single destination folder**. Repeat the flag as many times as you need — the last positional argument is the destination:

```bash
spotiflac -p https://open.spotify.com/playlist/AAA \
          -p https://open.spotify.com/playlist/BBB \
          -p https://open.spotify.com/playlist/CCC \
          ./Music --service ext:tidal-web
```

- **One copy per track.** A song that appears in three of those playlists is downloaded once. Tracks are matched by ISRC (resolved automatically when the metadata lacks it), falling back to artist + title, so the same recording pulled from different playlists is recognised even when the catalogue ids differ.
- **Nothing already on disk is downloaded again.** The destination folder is indexed before any extension is contacted, in *any* audio format — a track already there as `.m4a` is not re-fetched just because this run would produce a `.flac`.
- **One M3U per playlist.** Each playlist gets a `<Playlist Name>.m3u8` file in the destination folder listing its own tracks, in playlist order, with paths relative to the folder — so the whole directory stays portable and can be copied to a phone or a USB stick as is. Two playlists sharing a name get `Name.m3u8` and `Name (2).m3u8`.
- **Cheap to re-run.** Playlist files are rewritten only when their content actually changed. Running the same command again after a playlist gained a track downloads that one track and touches that one M3U file.

Tracks that failed to download are left out of the playlist file, so it always lists files that really exist; they are picked up on the next run.

Everything else keeps working as usual — `--mp3`, `--filename-format`, `--service`, `--retries` and friends all apply:

```bash
# Sync three playlists as 320 kbps MP3, writing classic .m3u files
spotiflac -p URL1 -p URL2 -p URL3 ./Music --mp3 --m3u m3u
```

With `--mp3` a playlist entry points at the converted file, and a track already present as MP3 is skipped without any network request. Use `--m3u none` to merge the playlists into one folder without writing playlist files at all.

> **Note:** avoid `--use-track-numbers` (and `{position}` in `--filename-format`) here: the number depends on the merged playlist order, so filenames would change whenever any playlist does — and previously downloaded tracks would be fetched again under the new name. SpotiFLAC warns when you do.

### Post-Download Actions

| Action | Description |
|---|---|
| `none` | Do nothing (default) |
| `open_folder` | Open the output folder in the system file manager |
| `notify` | Send an OS desktop notification with a summary |
| `command` | Run a custom shell command — placeholders: `{folder}`, `{succeeded}`, `{failed}` (quote `{folder}` in your template, e.g. `'{folder}'`, to handle spaces; this does not protect against an apostrophe inside the path itself) |

```python
SpotiFLAC(url="...", output_dir="./downloads", post_download_action="open_folder")

SpotiFLAC(url="...", output_dir="./downloads",
          post_download_action="command",
          post_download_command="rsync -av '{folder}/' user@nas:/music/")
```

```bash
spotiflac https://... ./out --post-action notify
spotiflac https://... ./out --post-action command --post-command "rsync -av '{folder}/' user@nas:/music/"
```

> **Note:** Wrap `{folder}` in single quotes in your command template (e.g. `'{folder}'`) to safely handle spaces and most special characters. Single quotes do not protect against an apostrophe (`'`) inside the output path itself — avoid apostrophes in `output_dir`, or escape them manually for your shell before running the command.

### Discography Download

Download the complete discography of an artist. Duplicate tracks (same ISRC across different releases) are automatically skipped.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(url="https://open.spotify.com/artist/ARTIST_ID", output_dir="./MusicLibrary",
          services=["ext:qobuz-web", "ext:tidal-web"], use_album_subfolders=True,
          filename_format="{year} - {album}/{track}. {title}")
```

```bash
spotiflac https://open.spotify.com/artist/... ./MusicLibrary \
  --service ext:tidal-web --include-featuring \
  --use-album-subfolders --filename-format "{year} - {album}/{track}. {title}"
```

Recommended layout: `--use-album-subfolders` + `--filename-format "{year} - {album}/{track}. {title}"`.

### Custom Output Path (single tracks)

For single track downloads you can specify the exact file path instead of relying on `output_dir` + `filename_format`.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/TRACK_ID",
    output_dir="./downloads",
    output_path="files/song.flac"
)
```

> **Note:** `output_path` is automatically ignored when the URL points to an album, playlist, or artist/discography.

### Passing Settings to an Extension (e.g. a self-hosted API instance)

`qobuz_local_api_url` and `tidal_custom_api` (and equivalents you'll find documented by other extensions) are **not** built-in behaviors of the core — they are optional settings forwarded to whichever extension you have installed for that service, if that extension supports them. Whether they do anything at all, what they connect to, and what account or credentials they expect depends entirely on the specific extension's own documentation and implementation, which the maintainer of this repository does not control or vouch for.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/TRACK_ID",
    output_dir="./downloads",
    services=["ext:tidal-web"],
    tidal_custom_api="https://your-instance.example.com",
)
```

```bash
spotiflac https://open.spotify.com/track/... ./downloads \
  --service ext:tidal-web \
  --tidal-api "https://your-instance.example.com"
```

> **Note:** These values are also saved and restored when using `--save-profile` / `--profile`.

---

## CLI Usage (standalone executables)

```bash
./SpotiFLAC-Windows.exe url
                        output_dir
                        [--service ext:<id> [ext:<id> ...]]
                        [--filename-format "{title} - {artist}"]
                        [--output-path "files/song.flac"]
                        [--quality LOSSLESS]
                        [--use-track-numbers]
                        [--use-album-track-numbers]
                        [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--first-artist-only]
                        [--qobuz-local-api URL]
                        [--tidal-api URL]
                        [--timeout seconds]
                        [--loop minutes]
                        [--no-extensions-fallback]
                        [--verbose]
                        [--no-lyrics]
                        [--lyrics-providers spotify apple musixmatch amazon lrclib]
                        [--no-enrich]
                        [--enrich-providers deezer apple qobuz tidal soundcloud]
                        [--retries N]
                        [--post-action none|open_folder|notify|command]
                        [--post-command "CMD with {folder} {succeeded} {failed}"]
                        [--profile NAME]
                        [--save-profile NAME]
```

```bash
chmod +x SpotiFLAC-Linux-arm64
./SpotiFLAC-Linux-arm64 url
                        output_dir
                        [--service ext:<id> [ext:<id> ...]]
                        [--filename-format "{title} - {artist}"]
                        [--output-path "files/song.flac"]
                        [--quality LOSSLESS]
                        [--use-track-numbers]
                        [--use-album-track-numbers]
                        [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--first-artist-only]
                        [--qobuz-local-api URL]
                        [--tidal-api URL]
                        [--timeout seconds]
                        [--loop minutes]
                        [--no-extensions-fallback]
                        [--verbose]
                        [--no-lyrics]
                        [--lyrics-providers spotify apple musixmatch amazon lrclib]
                        [--no-enrich]
                        [--enrich-providers deezer apple qobuz tidal soundcloud]
                        [--retries N]
                        [--post-action none|open_folder|notify|command]
                        [--post-command "CMD with {folder} {succeeded} {failed}"]
                        [--profile NAME]
                        [--save-profile NAME]
```

*(For ARM devices like Raspberry Pi, replace `x86_64` with `arm64`)*

> **Reminder:** `--service` values only resolve to something functional if you have already installed a matching extension (`--service ext:tidal-web` needs the `tidal-web` extension installed from a registry you configured). See [JavaScript Extensions](#javascript-extensions).

---

## API Reference

### `SpotiFLAC()` Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | `str` / `list[str]` | Required | A single URL or a list of URLs (batch mode). |
| `output_dir` | `str` | Required | The destination directory path where the audio files will be saved. |
| `output_path` | `str` | `None` | Exact destination file path for single track downloads. Overrides `output_dir` + `filename_format`. Automatically ignored for albums, playlists and artist discographies. |
| `services` | `list` | `["ext:tidal-web"]` | Extensions to use and their priority order, as `ext:<id>` (or a legacy alias — see [JavaScript Extensions](#javascript-extensions)). Each `id` must correspond to an extension you have already installed; nothing is bundled or installed automatically. |
| `filename_format` | `str` | `"{title} - {artist}"` | Format for naming downloaded files. See placeholders below. |
| `use_track_numbers` | `bool` | `False` | Prefixes the filename with the track number. |
| `use_album_track_numbers` | `bool` | `False` | Uses the track's original album number instead of the download queue position. |
| `use_artist_subfolders` | `bool` | `False` | Automatically organizes downloaded files into subfolders by artist. |
| `use_album_subfolders` | `bool` | `False` | Automatically organizes downloaded files into subfolders by album. |
| `first_artist_only` | `bool` | `False` | Uses only the first artist in tags and filename. |
| `include_featuring` | `bool` | `False` | When downloading an artist discography, also includes tracks where the artist appears as a featured artist. |
| `tidal_custom_api` | `str` | `None` | Optional setting forwarded to the installed `tidal-web`-family extension, if it supports it. Has no effect on its own — see [Passing Settings to an Extension](#passing-settings-to-an-extension-eg-a-self-hosted-api-instance). |
| `timeout_s` | `int` | `None` | Per-track download timeout in seconds. If a single track download does not complete within this time, the process is terminated and the track is marked as failed. SpotiFLAC then moves on to the next extension or retry. Set to `None` (default) to disable the timeout. |
| `loop` | `int` | `None` | Duration in minutes to keep retrying permanently failed tracks after a full session completes. |
| `track_max_retries` | `int` | `0` | Extra download attempts per track when all extensions fail on the first try. Each retry cycles through all configured extensions again with exponential backoff (2 s → 4 s → 8 s …, capped at 30 s). |
| `quality` | `str` | `"LOSSLESS"` | Requested audio quality. Supported values depend on the installed extension. |
| `allow_fallback` | `bool` | `True` | Automatically falls back to the next available quality tier if the requested quality is unavailable. |
| `log_level` | `int` | `logging.WARNING` | Python logging level. |
| `embed_lyrics` | `bool` | `True` | Whether to fetch and embed synchronized lyrics (LRC) into the audio file. |
| `lyrics_providers` | `list` | `["spotify", "apple", "musixmatch", "lrclib", "amazon"]` | Priority order of lyrics providers to attempt. |
| `enrich_metadata` | `bool` | `True` | Enables multi-provider metadata enrichment (HD covers, BPM, labels, etc.). |
| `enrich_providers` | `list` | `["deezer", "apple", "qobuz", "tidal", "soundcloud"]` | Priority order of metadata providers to attempt. |
| `qobuz_local_api_url` | `str` | `None` | Optional setting forwarded to the installed `qobuz-web`-family extension, if it supports it. Has no effect on its own — see [Passing Settings to an Extension](#passing-settings-to-an-extension-eg-a-self-hosted-api-instance). |
| `use_extensions_fallback` | `bool` | `True` | Whether to automatically fall back to another installed extension for the same alias if one fails. Set to `False` to use only the extensions explicitly listed in `services`. |
| `transcode_to` | `str` | `None` | Converts every finished track to this format. Currently only `"mp3"` (see [MP3 Transcoding](#mp3-transcoding)). `None` keeps the extension's original format. Requires `ffmpeg`. |
| `transcode_bitrate` | `str` | `"320k"` | Bitrate used by `transcode_to`, e.g. `"320k"`, `"256k"`, `"192k"`. |
| `transcode_keep_original` | `bool` | `False` | Keeps the original lossless file next to the converted one. By default the source is deleted once the conversion succeeds. |
| `post_download_action` | `str` | `"none"` | Action after all downloads finish: `"none"`, `"open_folder"`, `"notify"`, `"command"`. |
| `post_download_command` | `str` | `""` | Shell command to run when `post_download_action="command"`. Supports `{folder}`, `{succeeded}`, `{failed}` placeholders; quote `{folder}` in your template (e.g. `'{folder}'`) since the substituted path may contain spaces. |

### Filename Format Placeholders

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

### CLI Flag Reference

| Flag | Short | Default | Description |
|---|---|---|---|
| `--service` | `-s` | `ext:tidal-web` | One or more extensions in priority order, as `ext:<id>` (or a legacy alias resolved to an installed extension — see [JavaScript Extensions](#javascript-extensions)). |
| `--filename-format` | `-f` | `{title} - {artist}` | Filename template with placeholders. |
| `--output-path` | `-o` | `None` | Exact output file path for single track downloads. Ignored for albums, playlists and discographies. |
| `--quality` | `-q` | `LOSSLESS` | Requested audio quality. Supported values depend on the installed extension. |
| `--use-track-numbers` | | `False` | Prefix filenames with track numbers. |
| `--use-album-track-numbers` | | `False` | Use the track's original album number instead of queue position. |
| `--use-artist-subfolders` | | `False` | Organize files into per-artist subfolders. |
| `--use-album-subfolders` | | `False` | Organize files into per-album subfolders. |
| `--first-artist-only` | | `False` | Use only the first artist in tags and filename. |
| `--include-featuring` | | `False` | Include tracks where the artist appears as a featured artist. Only applies to artist/discography URLs. |
| `--qobuz-local-api` | | `None` | Optional setting forwarded to the installed Qobuz extension, if it supports it. |
| `--tidal-api` | | `None` | Optional setting forwarded to the installed Tidal extension, if it supports it. |
| `--timeout` | | `None` | Per-track download timeout in seconds. If a track download stalls or takes longer than this limit, it is forcibly terminated and marked as failed, then SpotiFLAC moves to the next extension or retry. |
| `--loop` | `-l` | `None` | Keep retrying permanently failed tracks every N minutes. |
| `--no-extensions-fallback` | | `False` | Disable automatic fallback to another installed extension for the same alias when one fails (fallback is enabled by default). |
| `--retries` | | `0` | Extra per-track download attempts on failure. Cycles through all configured extensions with exponential backoff. |
| `--playlist` | `-p` | `None` | Playlist URL to sync; repeat once per playlist. All tracks go to a single destination folder, shared tracks are downloaded once, and each playlist gets its own M3U file (see [Multiple Playlists in One Folder](#multiple-playlists-in-one-folder)). |
| `--m3u` | | `m3u8` | Playlist file written for each `--playlist`: `m3u8`, `m3u` or `none`. Rewritten only when its content changed. |
| `--transcode` | | `none` | Convert every downloaded track to this format: `none` or `mp3`. Requires `ffmpeg`. |
| `--mp3` | | | Shorthand for `--transcode mp3`. |
| `--transcode-bitrate` | | `320k` | Bitrate used by `--transcode`, e.g. `320k`, `256k`, `192k`. |
| `--keep-original` | | `False` | Keep the original lossless file alongside the transcoded one. |
| `--verbose` | `-v` | `False` | Enable debug logging. |
| `--no-lyrics` | | `False` | Disable lyrics embedding (lyrics are embedded by default). |
| `--lyrics-providers` | | `spotify apple musixmatch lrclib amazon` | Lyrics provider priority order. |
| `--no-enrich` | | `False` | Disable multi-provider metadata enrichment (enrichment is enabled by default). |
| `--enrich-providers` | | `deezer apple qobuz tidal soundcloud` | Metadata enrichment provider priority order. |
| `--post-action` | | `none` | Action after all downloads finish: `none`, `open_folder`, `notify`, `command`. |
| `--post-command` | | `""` | Shell command for `--post-action=command`. Placeholders: `{folder}`, `{succeeded}`, `{failed}`; quote `{folder}` in your template (e.g. `'{folder}'`) since the substituted path may contain spaces. |
| `--profile` | | `None` | Load a saved profile. CLI flags override profile values. |
| `--save-profile` | | `None` | Save current CLI configuration as a named profile after the run. |

---

## MusicBrainz Enrichment

SpotiFLAC automatically queries MusicBrainz in the background (when an ISRC is available) while the audio is being downloaded, adding professional-grade tags at no extra time cost. Fields written when found:

| Tag | Description |
|---|---|
| `GENRE` | Genre(s), sorted by popularity (up to 5) |
| `BPM` | Beats per minute |
| `LABEL` / `ORGANIZATION` | Record label name |
| `CATALOGNUMBER` | Catalog number |
| `BARCODE` | Release barcode / UPC |
| `ORIGINALDATE` / `ORIGINALYEAR` | First-ever release date |
| `RELEASECOUNTRY` | Country of release |
| `RELEASESTATUS` | Release status (e.g. Official) |
| `RELEASETYPE` | Release type (e.g. Album, Single) |
| `MEDIA` | Media format (e.g. CD, Digital Media) |
| `SCRIPT` | Script of the release text |
| `ARTISTSORT` | Artist sort name for file managers |
| `MUSICBRAINZ_TRACKID` | MusicBrainz recording ID |
| `MUSICBRAINZ_ALBUMID` | MusicBrainz release ID |
| `MUSICBRAINZ_ARTISTID` | MusicBrainz artist ID |
| `MUSICBRAINZ_RELEASEGROUPID` | MusicBrainz release group ID |
| `MUSICBRAINZ_ALBUMARTISTID` | MusicBrainz album artist ID |
| `ALBUMARTISTSORT` | Album artist sort name for file managers |

---

## Download Validation

After each download, SpotiFLAC validates the file to detect common issues:

- **Preview detection** — if the expected duration is ≥ 60 s but the downloaded file is ≤ 35 s, the file is deleted and the download is retried with the next extension.
- **Duration mismatch** — for tracks longer than 90 s, a deviation greater than 25% (or 15 s minimum) from the expected duration is treated as a corrupt download and the file is removed.

---

## Want to support the project?

If this software is useful and brings you value, consider supporting the project by buying us a coffee. Your support helps keep development going.

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/bartolomeorusso9)

---

## API Credits

[Song.link](https://song.link) · [MusicBrainz](https://musicbrainz.org) · [LRCLIB](https://lrclib.net) · [Musixmatch](https://www.musixmatch.com) · [iTunes Search API](https://itunes.apple.com)

> Provider-specific credits (Tidal, Qobuz, Amazon Music, Deezer, SoundCloud, Apple Music, Pandora, and any third-party API used to reach them) now belong to whichever extension you install — see that extension's own documentation for its credits and terms.

> **[!TIP]**
> Star the repo to show support, and click **Watch → Custom → Releases** on GitHub if you want to be notified as soon as a new release goes out.
# SpotiFLAC Python Module

Fetch Spotify track metadata and retrieve matching lossless audio through Tidal, Qobuz, Amazon Music and other provider backends — supplied entirely via extensions you choose and configure yourself. Integrate directly into your Python projects, build custom Telegram bots, automation tools, or bulk downloaders.

[![GitHub stars](https://img.shields.io/github/stars/BartolomeoRusso9/SpotiFLAC-Module-Version?color=ffcb47&labelColor=black&logo=github&label=Stars)](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/stargazers)
[![Latest release](https://img.shields.io/github/v/release/BartolomeoRusso9/SpotiFLAC-Module-Version?color=8b5cf6&labelColor=black&logo=github&label=Latest%20Release)](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/releases/latest)
[![PyPI version](https://img.shields.io/pypi/v/spotiflac?logo=pypi&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/)
[![Python versions](https://img.shields.io/pypi/pyversions/spotiflac?logo=python&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/)
[![GitHub downloads](https://img.shields.io/github/downloads/BartolomeoRusso9/SpotiFLAC-Module-Version/total?color=22c55e&labelColor=black&logo=github&label=Downloads)](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/releases)
[![PyPI downloads](https://img.shields.io/pepy/dt/spotiflac?logo=pypi&logoColor=ffffff&labelColor=000000)](https://pypi.org/project/SpotiFLAC/)
[![Telegram community](https://img.shields.io/badge/Telegram%20Community-369eff?labelColor=black&logo=telegram&logoColor=white)](https://t.me/SpotiFLAC_Chat)

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
>
> - [SpotiFLAC (Desktop)](https://github.com/afkarxyz/SpotiFLAC) — Download music in true lossless FLAC from different providers for Windows, macOS & Linux
> - [SpotiFLAC (Mobile)](https://github.com/zarzet/SpotiFLAC-Mobile) — SpotiFLAC for Android & iOS, maintained by [@zarzet](https://github.com/zarzet)

---

## Why the module (instead of the standalone apps)

The Desktop and Mobile apps are built for direct, immediate use: open it, paste a link, download. The Python module exists for a different case — **integrating** this logic into something else.

It makes sense to start here if:

- **You're building a bot** (Telegram, Discord) or a service that needs to handle requests from many users automatically, not a single manual download.
- **You need the async API** (`AsyncSpotiFLAC`) inside an existing FastAPI/Quart/Sanic app, with shared connection pooling.
- **You want to orchestrate bulk downloads** via scripts — multiple playlists, full discographies, local library retagging — with custom logic (filenames via a Python function, post-download actions, saved profiles).
- **You need to run headless**, on a server, a NAS, or inside Docker, possibly as part of a larger pipeline.
- **You want full control over which extensions get loaded** and from which registry, instead of relying on a fixed set baked into an app.

If you just want a GUI for personal use, with no code involved, the [Desktop](https://github.com/afkarxyz/SpotiFLAC) or [Mobile](https://github.com/zarzet/SpotiFLAC-Mobile) apps remain the simpler choice — this module is the building block those (and similar projects) can be built on top of.

---

## Features

- Native synchronous and asynchronous Python APIs
- Modular JavaScript and Python Extension system (bring-your-own registry — nothing bundled)
- Automatic fallback among the extensions *you* have installed
- Built-in GUI, as a native window or served locally in a browser (`--gui` / `--web`)
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

> **Important:** out of the box, SpotiFLAC does nothing but resolve Spotify metadata — it ships with **no built-in provider and no default extension source**. Before you can download anything, you need to point it at an extension registry of your own choosing and install at least one extension. See [Extensions](#extensions) below.

---

## Quick Start

SpotiFLAC can be used in multiple ways. Choose the mode that fits your needs.

### GUI Mode (recommended for most users)

Launch the graphical user interface with the `--gui` flag:

```bash
spotiflac --gui
```

*(Or `python launcher.py --gui` if running from source)*

### Web Mode (same GUI, in your browser)

Runs the exact same interface as `--gui`, served as a local web server instead of a native window — open it at `http://127.0.0.1:8000` (or whatever host/port you choose) in any browser:

```bash
spotiflac --web
```

*(Or `python launcher.py --web` if running from source)*

Binds to `127.0.0.1` (this machine only) by default. Override with `--host`/`--port` if needed — see the [CLI Flag Reference](#cli-flag-reference) below. Useful for running the GUI on a headless machine, inside Docker without a virtual display, or just preferring a browser tab over a native window.

> **Security note:** binding `--host` to anything other than `127.0.0.1`/`localhost` (e.g. `0.0.0.0`, or a LAN address) exposes the GUI — including endpoints that trigger downloads — to anyone who can reach that address, with no authentication of any kind. Only do this deliberately, on a network you trust, and consider putting it behind your own authentication (a reverse proxy, VPN, etc.) if you do.

### Interactive Mode (step-by-step wizard)

SpotiFLAC features a smart Interactive Wizard that guides you step-by-step. To launch the wizard, use the `--interactive` flag:

```bash
spotiflac --interactive
```

*(Or `python launcher.py --interactive` if running from source)*

On launch it automatically runs a lyrics-provider health check before asking any questions, so you always know which of your configured lyric sources are reachable.

**What the wizard does at startup:**

- **Lyrics Provider Health Check** — probes the configured lyric endpoints and shows availability inline (✅ / ❌) before asking anything
- **URL History** — shows your last 8 downloads so you can re-run one with a single keypress
- **Folder Memory** — remembers your last output directory and offers it as the default
- **Profile Load** — optionally restores a full saved configuration

**Smart URL Detection:** If you input an Artist URL, it will ask if you want to download "Featuring" tracks. It skips this question for albums or playlists.

**Smart File Paths:** If you input a Single Track URL, it will ask if you want to set a specific `.flac` output path. If you do, it intelligently skips all questions about filename formatting and subfolder organization.

**Unified Quality Profiles:** Choose `HI_RES_LOSSLESS` for the best available lossless tier or `LOSSLESS` for standard lossless audio. SpotiFLAC translates either profile into each provider's native quality token; lossy-only services use their best available audio.

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
| --- | --- |
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

## Extensions

SpotiFLAC has **no built-in download provider and no default extension source**. Every provider — Tidal, Qobuz, Amazon Music, Deezer, or anything else — is supplied entirely by extensions that you find, review, and choose to install yourself. Two extension runtimes are supported:

- **JavaScript** — sharing the same extension format used by [SpotiFLAC Mobile](https://github.com/zarzet/SpotiFLAC-Mobile), executed via a Node.js bridge.
- **Python** — packaged as `.spotiflac-ext` / `.sflx` files (a ZIP containing a manifest and a Python entry point), loaded directly in-process.

Extensions are never fetched or installed automatically. You must explicitly configure a registry before SpotiFLAC will contact anything. There are several equivalent ways to do it — pick whichever fits your workflow:

**Environment variable:**

```bash
# Comma-separated list of registry JSON URLs — none is set by default
export SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json"
```

**`.env` file** (see `.env.example`):

```env
SPOTIFLAC_REGISTRIES=https://example.com/my-registry.json
```

**CLI flag** (`--registries`, repeat once per URL) — persisted to `~/.spotiflac/registry_settings.json`, so you only need to pass it once and it's picked up on every future run, exactly like a registry added from the GUI/Interactive wizard:

```bash
spotiflac --registries https://example.com/my-registry.json URL ./out
```

**Python API** (`registries` parameter on `SpotiFLAC()` / `AsyncSpotiFLAC()`) — persisted the same way as the CLI flag:

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/...",
    output_dir="./downloads",
    registries=["https://example.com/my-registry.json"],
)
```

```python
from SpotiFLAC import AsyncSpotiFLAC

async with AsyncSpotiFLAC(
    output_dir="./downloads",
    registries=["https://example.com/my-registry.json"],
) as client:
    await client.download_track("https://open.spotify.com/track/...")
```

**Interactive wizard / GUI** — both expose a registry manager (add, remove, list, and see where each URL came from) without touching environment variables or files by hand.

All of these feed into the same merged, deduplicated list (`extensions.registry_config.effective_urls()`), regardless of which entry point you use — the CLI, the GUI, and the Python API all end up contacting the same registries.

Once configured, you can also install and manage extensions directly:

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
>
> - **Linux:** apt-get, dnf, yum, pacman
> - **macOS:** brew
> - **Windows:** winget, choco

**A note on legacy names:** for backwards compatibility, short names like `tidal`, `qobuz`, `amazon`, `deezer`, `apple`, `soundcloud`, `youtube`, `pandora` are still accepted in `services`/`--service`, and are resolved to an installed extension with a matching ID (e.g. `tidal` → `ext:tidal-web`) if — and only if — you have that extension installed. They are aliases, not built-in providers; nothing downloads without an extension behind it.

The maintainer does not review, endorse, or take responsibility for the content or behavior of any third-party registry or extension. Choose your sources with the same care you would apply to installing any other third-party code.

### Developing Extensions

- **JavaScript extensions** reuse the format built for [SpotiFLAC Mobile](https://github.com/zarzet/SpotiFLAC-Mobile). Its [Extension Development Guide](https://github.com/spotiflacapp/SpotiFLAC-Mobile/blob/main/docs/EXTENSION_DEVELOPMENT.md) is the closest available reference, but it was written for Mobile — some details (packaging, available runtime capabilities) may not match this project exactly. Verify against this repository's own loader (`SpotiFLAC/extensions/runtime.py`) before relying on it.
- **Python extensions** are ZIP packages (`.spotiflac-ext` / `.sflx`) containing a manifest and a Python module, loaded directly by `SpotiFLAC/extensions/python_provider.py`. There's no separate guide yet — reading that file, and an existing extension's manifest, is currently the best way to see the expected shape.

If you build something reusable, consider publishing it to your own registry rather than asking the maintainer to bundle or endorse it — see [Extensions](#extensions) above for why nothing is bundled by design.

---

## Docker Usage & Headless Automation

A lightweight, CLI-focused Docker image is available for running SpotiFLAC on servers, NAS devices, or any headless environment.

### Build the Image

```bash
docker build -t spotiflac .
```

### Basic Docker Usage

The image runs a virtual display (Xvfb) and exposes it over VNC — some installed extensions may rely on a headless browser internally. Map port `6080` (web VNC viewer) and set `--shm-size=1g`, or the browser-dependent parts may crash:

Run a download by mounting local directories to persist your downloads, configuration, cache, and extension registry across container restarts. Remember to also pass `SPOTIFLAC_REGISTRIES` (via `-e` or an `.env` file) since none is configured by default:

```bash
docker run --rm -it \
  -p 6080:6080 \
  --shm-size=1g \
  -e SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json" \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/.spotiflac_docker:/root/.spotiflac" \
  -v "$(pwd)/.cache_docker:/root/.cache/spotiflac" \
  spotiflac "https://open.spotify.com/track/TRACK_ID" \
  /app/downloads -s ext:deezer-web -q LOSSLESS
```

Open `http://localhost:6080/vnc.html` in a browser to watch the virtual screen live, if needed. Set `X11VNC_PASSWORD` (env var, see `.env.example`) to protect the VNC session with a password; if unset, it starts without one.

### Web Mode in Docker (lighter alternative to VNC)

If you just want the GUI itself over the network — not a live view of a virtual desktop — `--web` mode needs none of the above. The entrypoint detects `--web` and skips Xvfb/Fluxbox/VNC entirely, so the container starts faster and uses less memory:

```bash
docker run --rm -it \
  -p 8000:8000 \
  -e SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json" \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/.spotiflac_docker:/root/.spotiflac" \
  -v "$(pwd)/.cache_docker:/root/.cache/spotiflac" \
  spotiflac --web --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser.

> **Note:** `--host 0.0.0.0` is required here — the CLI default (`127.0.0.1`) would only accept connections from inside the container itself, unreachable from the host. This also means the GUI is reachable by anything that can reach the mapped port, with no authentication (see the security note under [Web Mode](#web-mode-same-gui-in-your-browser)). Only publish the port on a network you trust, or put it behind your own authentication/reverse proxy.

### Published Image (GHCR)

Official Docker images are published on GitHub Container Registry (GHCR), allowing you to run the latest version without building locally.

```bash
docker pull ghcr.io/bartolomeorusso9/spotiflac-module-version:latest
```

### Logs in Headless Environments

A progress bar is a stream of carriage returns: readable on a terminal, unreadable in a log file. `docker logs` collapses each refresh into a `[285B blob data]` line, which buries everything worth reading.

SpotiFLAC therefore draws animated bars only when stderr is an interactive terminal. Everywhere else — Docker, cron, a redirected file — it prints the same information as plain lines instead:

```text
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
| --- | --- |
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

### Lyrics Provider Health Check

SpotiFLAC can probe the endpoints of the configured lyrics providers before embedding lyrics, to verify which lyric sources are currently reachable.

This check is specifically about lyrics sources, not audio download providers. In Interactive Mode it runs automatically at startup. In code or scripts you can call it directly:

```python
from SpotiFLAC.core.health_check import (
    run_health_check,
    print_health_report,
    get_working_providers,
)

import asyncio
from SpotiFLAC.core.health_check import (
    run_health_check,
    print_health_report,
    get_working_providers,
)

async def main():
    results = await run_health_check(["ext:tidal-web", "ext:qobuz-web", "ext:deezer-web"])
    print_health_report(results)

    working = get_working_providers(results)
    print("Available providers:", working)

asyncio.run(main())
```

```bash
# CLI: check installed extensions then download
spotiflac https://open.spotify.com/track/... ./out --service ext:tidal-web ext:qobuz-web
```

The health check runs in parallel with a configurable timeout (default: 5 s per endpoint) and never blocks your download if a check fails. In the GUI, the check reports provider-level availability and endpoint counts, without exposing individual raw endpoint URLs.

### Configuration Profiles

Save and reuse complete download configurations without re-typing them every time.

### Save a profile

```bash
# Save current flags as "hires-tidal"
spotiflac https://... ./out \
  --service ext:tidal-web \
  --quality HI_RES_LOSSLESS \
  --use-album-subfolders \
  --filename-format "{year} - {album}/{track}. {title}" \
  --save-profile hires-tidal
```

### Load a profile

```bash
# Load "hires-tidal" — flags override profile values when both are present
spotiflac https://... ./out --profile hires-tidal
```

### In Python

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

Downloads use the selected quality profile: `HI_RES_LOSSLESS` requests the best available lossless tier, while `LOSSLESS` requests standard lossless audio. Set `transcode_to="mp3"` (Python) or `--mp3` / `--transcode mp3` (CLI) to convert every finished track to MP3 — 320 kbps by default — for players or car stereos that cannot handle lossless files. Tags, cover art and lyrics are carried over to the MP3, and the original file is deleted once the conversion succeeds unless `transcode_keep_original` / `--keep-original` is set.

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

### Hi-Res Verification

Enable `verify_hires=True` (Python) or `--verify-hires` (CLI) to run a spectral-analysis QA check on every successful lossless download, flagging files that declare a high sample rate (e.g. 96 kHz) but whose actual audio content stops well short of it — a common fingerprint of **upsampling**: taking a CD-quality or lossy source and re-encoding it at a higher sample rate without adding any real high-frequency content, so it *looks* like Hi-Res without being one.

```bash
spotiflac https://open.spotify.com/album/... ./out --service ext:tidal-web -q HI_RES_LOSSLESS --verify-hires
```

```python
from SpotiFLAC import SpotiFLAC
SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["ext:tidal-web"],
    quality="HI_RES_LOSSLESS",
    verify_hires=True,
)
```

**How it works:** for each finished track, a short segment (default 30s) is decoded from the middle of the file — never the whole track, to keep memory usage bounded — and its average frequency spectrum is compared against the noise floor. If the file's sample rate implies Hi-Res but no real content is found above ~24 kHz, a warning is printed and logged; nothing else happens.

**Design notes worth knowing before you turn it on:**

- **Off by default and fully opt-in.** It requires the optional `librosa` and `numpy` packages, which are *not* installed by default — install them with `pip install librosa numpy` or `pip install SpotiFLAC[hires]`. If they're missing, the check is silently skipped (a debug-level log line, nothing more) rather than breaking your run.
- **Never blocks or fails a download.** The check runs as a background task *after* the file has already been saved successfully — a track download is never delayed, retried, or marked as failed because of it, and analysis errors (corrupt segment, unreadable file, etc.) are swallowed and logged at debug level, not surfaced as errors.
- **A finding is a hint, not a certification.** Some genuine Hi-Res masters are deliberately low-pass filtered during mastering (common in pop/rock) and will still read as "no anomaly". Treat a "possibly upsampled" warning as something worth a closer listen, not definitive proof.
- **Skipped automatically for lossy output.** If `transcode_to="mp3"` (or `--mp3`) is set, the already-lossy result is never analyzed — checking an MP3 for ultrasonic content would be meaningless.
- **Standalone tool.** The underlying checker also ships as a CLI you can point at any file(s) you already have, independent of a download run:

  ```bash
  python -m SpotiFLAC.tools.hires_check_cli "My Track.flac" --seconds 45
  ```

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
| --- | --- |
| `none` | Do nothing (default) |
| `open_folder` | Open the output folder in the system file manager |
| `notify` | Send an OS desktop notification with a summary |
| `command` | Run a custom shell command — placeholders: `{folder}`, `{succeeded}`, `{skipped}`, `{failed}` (quote `{folder}` in your template, e.g. `'{folder}'`, to handle spaces; this does not protect against an apostrophe inside the path itself) |

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

## Local Tagging

Improve your existing music library by automatically matching local audio files against Spotify metadata and applying professional-grade tags. This is useful for:

- Fixing incomplete or incorrect tags on older ripped CDs or downloads
- Enriching a library with album art, genres, BPM, ISRCs, and other metadata from Spotify and MusicBrainz
- Bulk-updating hundreds of files in a single operation

The Local Tagging system works in **three phases**:

1. **Scan** — reads all audio files in a folder, extracts their current tags, and (for files with no tags) guesses artist/title from the filename
2. **Match** — searches for each file using the extracted or guessed metadata, returns ranked candidate matches from Spotify sorted by confidence
3. **Apply** — writes the chosen metadata to each file with automatic backup

### Supported Audio Formats

Scans and tags any format that SpotiFLAC can write: FLAC, MP3, M4A/AAC, OGG Vorbis, Opus, WAV, AIFF, WMA, WavPack, Monkey's Audio, Musepack, TrueAudio.

### Using the GUI / Web Interface

Open the **"Fix Local Files"** tab and follow the wizard:

1. **Choose a folder** — browse to your music directory (or drag & drop a folder onto the interface)
2. **Review matches** — SpotiFLAC scans, matches, and displays each file with up to 5 candidate matches, sorted by confidence (0–100)
3. **Select metadata** — for each file, choose which match to apply, or skip it entirely
4. **Preview changes** — see what tags will be written before applying
5. **Apply** — apply all changes at once with progress tracking and automatic per-file backup

Files with confidence ≥ 90% are marked as "safe to auto-apply"; files below that threshold are flagged for manual review.

### Using the Python API

```python
import asyncio
from SpotiFLAC.core.local_processor import (
    scan_and_match_async,
    retag_local_file_async,
    default_embed_options,
)
from SpotiFLAC.core.models import TrackMetadata

async def fix_library(folder_path: str) -> None:
    # Phase 1 & 2: scan folder and find matches for each file
    entries = await scan_and_match_async(
        folder_path,
        recursive=True,           # scan subdirectories too
        candidates_per_file=5,    # show top 5 matches
    )

    # Phase 3: apply metadata for each file
    embed_opts = default_embed_options()
    for entry in entries:
        if entry.best and entry.is_safe_match:  # confidence >= 90%
            result = await retag_local_file_async(
                file_path=str(entry.info.file_path),
                metadata=entry.best.metadata,
                options=embed_opts,
                backup=True,  # automatic .bak backup
            )
            if result.success:
                print(f"✓ {entry.info.file_path}: tagged successfully")
            else:
                print(f"✗ {entry.info.file_path}: {result.error}")

asyncio.run(fix_library("~/Music/MyLibrary"))
```

### Match Confidence & Safety

Matching uses a **string-similarity algorithm** that compares the file's title + artist against each Spotify search result:

- **Confidence ≥ 90%** — marked as "safe" and can be auto-applied without review
- **Confidence < 90%** — flagged for manual review to avoid mislabeling

The matching algorithm is heuristic and does not analyze audio content — it compares text only. A track with unusual spelling or featuring artists can have legitimate lower scores even when the match is correct. Always review before applying in bulk if you're unsure.

### Metadata Written

When applying a match, the following tags are written (previous tags are stripped):

- Standard tags: title, artist, album, album artist, date, disc, track number, genre
- Extended metadata: ISRC, BPM, labels, lyrics (if `embed_lyrics=True`)
- Cover art: highest-resolution available from Spotify and enrichment providers
- MusicBrainz enrichment: genre, BPM, organization/label, UPC (if available)

### Backup & Recovery

Every file gets an automatic `.bak` backup before tagging:

```text
MyTrack.flac         (original)
MyTrack.flac.bak     (backup)
```

If something goes wrong during the apply step, the backup is restored and the operation is rolled back for that file. You can also delete `.bak` files manually after confirming the results are correct.

### Per-File Customization

Fine-tune embedding options for specific use cases:

```python
from SpotiFLAC.core.tagger import EmbedOptions

custom_opts = EmbedOptions(
    embed_cover=True,
    embed_lyrics=True,
    lyrics_type="lrc",
    flac_compression_level=8,
)

result = await retag_local_file_async(
    file_path="song.flac",
    metadata=matched_metadata,
    options=custom_opts,
    backup=True,
)
```

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
                        [--artist-separator SEP]
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
                        [--post-command "CMD with {folder} {succeeded} {skipped} {failed}"]
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
                        [--post-command "CMD with {folder} {succeeded} {skipped} {failed}"]
                        [--profile NAME]
                        [--save-profile NAME]
```

*(For ARM devices like Raspberry Pi, replace `x86_64` with `arm64`)*

> **Reminder:** `--service` values only resolve to something functional if you have already installed a matching extension (`--service ext:tidal-web` needs the `tidal-web` extension installed from a registry you configured). See [Extensions](#extensions).

---

## API Reference

### `SpotiFLAC()` Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | `str` / `list[str]` | Required | A single URL or a list of URLs (batch mode). |
| `output_dir` | `str` | Required | The destination directory path where the audio files will be saved. |
| `output_path` | `str` | `None` | Exact destination file path for single track downloads. Overrides `output_dir` + `filename_format`. Automatically ignored for albums, playlists and artist discographies. |
| `services` | `list` | `["ext:tidal-web"]` | Extensions to use and their priority order, as `ext:<id>` (or a legacy alias — see [Extensions](#extensions)). Each `id` must correspond to an extension you have already installed; nothing is bundled or installed automatically. |
| `registries` | `list` | `None` | One or more extension-registry JSON URLs to add before the run, as an alternative to `SPOTIFLAC_REGISTRIES` or a `.env` file. Must be `https://`. Persisted to `~/.spotiflac/registry_settings.json` on first use, so subsequent runs (CLI, GUI, or Python) pick it up automatically without passing it again — see [Extensions](#extensions). |
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
| `tidal_custom_api` | `str` | `None` | Optional setting forwarded to the installed `tidal-web`-family extension, if it supports it. Has no effect on its own — see [Passing Settings to an Extension](#passing-settings-to-an-extension-eg-a-self-hosted-api-instance). |
| `timeout_s` | `int` | `None` | Per-track download timeout in seconds. If a single track download does not complete within this time, the process is terminated and the track is marked as failed. SpotiFLAC then moves on to the next extension or retry. Set to `None` (default) to disable the timeout. |
| `loop` | `int` | `None` | Duration in minutes to keep retrying permanently failed tracks after a full session completes. |
| `track_max_retries` | `int` | `0` | Extra download attempts per track when all extensions fail on the first try. Each retry cycles through all configured extensions again with exponential backoff (2 s → 4 s → 8 s …, capped at 30 s). |
| `quality` | `str` | `"LOSSLESS"` | Requested profile: `LOSSLESS` or `HI_RES_LOSSLESS`. Legacy provider-specific values are accepted and normalized. |
| `allow_fallback` | `bool` | `True` | For `HI_RES_LOSSLESS`, allows fallback to `LOSSLESS` when the higher-resolution tier is unavailable. It never downgrades lossless requests to compressed audio. |
| `log_level` | `int` | `logging.WARNING` | Python logging level. |
| `embed_lyrics` | `bool` | `True` | Whether to fetch and embed synchronized lyrics (LRC) into the audio file. |
| `lyrics_providers` | `list` | `["spotify", "apple", "musixmatch", "lrclib", "amazon"]` | Priority order of lyrics providers to attempt. |
| `enrich_metadata` | `bool` | `True` | Enables multi-provider metadata enrichment (HD covers, BPM, labels, etc.). |
| `enrich_providers` | `list` | `["deezer", "apple", "qobuz", "tidal", "soundcloud"]` | Priority order of metadata providers to attempt. |
| `qobuz_token` | `str` | `None` | Optional setting forwarded to the installed Qobuz extension, if it supports it. Has no built-in behavior of its own. |
| `qobuz_local_api_url` | `str` | `None` | Optional setting forwarded to the installed `qobuz-web`-family extension, if it supports it. Has no effect on its own — see [Passing Settings to an Extension](#passing-settings-to-an-extension-eg-a-self-hosted-api-instance). |
| `use_extensions_fallback` | `bool` | `True` | Whether to automatically fall back to another installed extension for the same alias if one fails. Set to `False` to use only the extensions explicitly listed in `services`. |
| `transcode_to` | `str` | `None` | Converts every finished track to this format. Currently only `"mp3"` (see [MP3 Transcoding](#mp3-transcoding)). `None` keeps the extension's original format. Requires `ffmpeg`. |
| `transcode_bitrate` | `str` | `"320k"` | Bitrate used by `transcode_to`, e.g. `"320k"`, `"256k"`, `"192k"`. |
| `transcode_keep_original` | `bool` | `False` | Keeps the original lossless file next to the converted one. By default the source is deleted once the conversion succeeds. |
| `verify_hires` | `bool` | `False` | Runs a spectral-analysis QA check after each successful lossless download, flagging files that declare a high sample rate but lack real content above standard-definition frequencies (possible upsampling / fake Hi-Res). Requires the optional `librosa`/`numpy` packages (`pip install SpotiFLAC[hires]`); silently skipped if they're not installed. Never fails or delays a download — see [Hi-Res Verification](#hi-res-verification). |
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
| `--service` | `-s` | `ext:tidal-web` | One or more extensions in priority order, as `ext:<id>` (or a legacy alias resolved to an installed extension — see [Extensions](#extensions)). |
| `--registries` | | `None` | An extension-registry JSON URL to add before running; repeat the flag for each one. Alternative to `SPOTIFLAC_REGISTRIES` or a `.env` file. Must be `https://`. Persisted to `~/.spotiflac/registry_settings.json`, so you only need to pass it once — see [Extensions](#extensions). |
| `--filename-format` | `-f` | `{title} - {artist}` | Filename template with placeholders. |
| `--output-path` | `-o` | `None` | Exact output file path for single track downloads. Ignored for albums, playlists and discographies. |
| `--quality` | `-q` | `LOSSLESS` | Requested profile: `LOSSLESS` or `HI_RES_LOSSLESS`. Legacy provider-specific values are accepted and normalized. |
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
| `--retries` | | `0` | Extra per-track download attempts on failure. Cycles through all configured extensions with exponential backoff. |
| `--max-concurrent` | | `2` | How many tracks to download at once. Each track still tries its providers in order/fallback on its own — this only controls how many tracks run simultaneously. Use `1` for fully sequential downloads with no interleaved console output. |
| `--playlist` | `-p` | `None` | Playlist URL to sync; repeat once per playlist. All tracks go to a single destination folder, shared tracks are downloaded once, and each playlist gets its own M3U file (see [Multiple Playlists in One Folder](#multiple-playlists-in-one-folder)). |
| `--m3u` | | `m3u8` | Playlist file written for each `--playlist`: `m3u8`, `m3u` or `none`. Rewritten only when its content changed. |
| `--transcode` | | `none` | Convert every downloaded track to this format: `none` or `mp3`. Requires `ffmpeg`. |
| `--mp3` | | | Shorthand for `--transcode mp3`. |
| `--transcode-bitrate` | | `320k` | Bitrate used by `--transcode`, e.g. `320k`, `256k`, `192k`. |
| `--keep-original` | | `False` | Keep the original lossless file alongside the transcoded one. |
| `--verify-hires` | | `False` | Runs a spectral-analysis QA check after each successful lossless download, flagging files that declare a high sample rate but lack real content above standard-definition frequencies (possible upsampling / fake Hi-Res). Requires the optional `librosa`/`numpy` packages (`pip install SpotiFLAC[hires]`); silently skipped if they're not installed. Never fails or delays a download — see [Hi-Res Verification](#hi-res-verification). |
| `--verbose` | `-v` | `False` | Enable debug logging. |
| `--no-lyrics` | | `False` | Disable lyrics embedding (lyrics are embedded by default). |
| `--lyrics-providers` | | `apple lrclib` | Lyrics provider priority order (CLI default; the Python API default is `spotify apple musixmatch lrclib amazon` when `lyrics_providers` is left unset). |
| `--no-enrich` | | `False` | Disable multi-provider metadata enrichment (enrichment is enabled by default). |
| `--enrich-providers` | | `deezer apple qobuz tidal soundcloud` | Metadata enrichment provider priority order. |
| `--post-action` | | `none` | Action after all downloads finish: `none`, `open_folder`, `notify`, `command`. |
| `--post-command` | | `""` | Shell command for `--post-action=command`. Placeholders: `{folder}`, `{succeeded}`, `{skipped}`, `{failed}`; quote `{folder}` in your template (e.g. `'{folder}'`) since the substituted path may contain spaces. |
| `--profile` | | `None` | Load a saved profile. CLI flags override profile values. |
| `--save-profile` | | `None` | Save current CLI configuration as a named profile after the run. |
| `--gui` | | `False` | Launch the GUI as a native window (pywebview). See [GUI Mode](#gui-mode-recommended-for-most-users). |
| `--web` | | `False` | Launch the same GUI as a local web server instead of a native window. See [Web Mode](#web-mode-same-gui-in-your-browser). |
| `--host` | | `127.0.0.1` | Host to bind `--web` to. Only change this deliberately — see the security note under [Web Mode](#web-mode-same-gui-in-your-browser). |
| `--port` | | `8000` | Port to bind `--web` to. |
| `--interactive` | | `False` | Launch the interactive step-by-step wizard. See [Interactive Mode](#interactive-mode-step-by-step-wizard). |

---

## MusicBrainz Enrichment

SpotiFLAC automatically queries MusicBrainz in the background (when an ISRC is available) while the audio is being downloaded, adding professional-grade tags at no extra time cost. Fields written when found:

| Tag | Description |
| --- | --- |
| `GENRE` | Genre |
| `ORGANIZATION` | Record label |
| `BPM` | Beats per minute |
| `UPC` | Release barcode / UPC |
| `ISRC` | Track ISRC code (normalized) |
| `ITUNESADVISORY` | Set to `1` when the release is marked explicit |

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
>
> **[!TIP]** Star the repo to show support, and click **Watch → Custom → Releases** on GitHub if you want to be notified as soon as a new release goes out.

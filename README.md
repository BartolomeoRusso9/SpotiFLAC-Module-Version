# SpotiFLAC Python Module

[![PyPI - Version](https://img.shields.io/pypi/v/spotiflac?style=for-the-badge&logo=pypi&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/) [![PyPI - Python Version](https://img.shields.io/pypi/pyversions/spotiflac?style=for-the-badge&logo=python&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/) [![Pepy Total Downloads](https://img.shields.io/pepy/dt/spotiflac?style=for-the-badge&logo=pypi&logoColor=ffffff&labelColor=000000)](https://pypi.org/project/SpotiFLAC/)

Integrate **SpotiFLAC** directly into your Python projects. Perfect for building custom Telegram bots, automation tools, bulk downloaders, downloading music for Jellyfin or web interfaces.

> **Looking for a standalone app?**
### [SpotiFLAC (Desktop)](https://github.com/afkarxyz/SpotiFLAC)

Download music in true lossless FLAC from different providers for Windows, macOS & Linux

### [SpotiFLAC (Mobile)](https://github.com/zarzet/SpotiFLAC-Mobile)

SpotiFLAC for Android & iOS — maintained by [@zarzet](https://github.com/zarzet)

---

## Installation

```bash
pip install SpotiFLAC
```

---
## Quick Start

SpotiFLAC can be used in multiple ways. Choose the mode that fits your needs:

### GUI Mode (Recommended for most users)

Launch the graphical user interface with the `--gui` flag:
```bash
spotiflac --gui
```
> (Or python launcher.py --gui if running from source)

### Interactive Mode (Step-by-step wizard)

SpotiFLAC features a smart Interactive Wizard that guides you step-by-step. To launch the wizard, use the `--interactive` flag:
```bash
spotiflac --interactive
```
> (Or python launcher.py --interactive if running from source)

On launch it automatically runs a **service health check** before asking any questions, so you always know which providers are reachable.

**What the wizard does at startup:**

1. **Service Health Check** — probes provider endpoints and shows provider availability inline (✅ / ❌) before asking anything
2. **URL History** — shows your last 8 downloads so you can re-run one with a single keypress
3. **Folder Memory** — remembers your last output directory and offers it as the default
4. **Profile Load** — optionally restores a full saved configuration

**Smart URL Detection:** If you input an Artist URL, it will ask if you want to download "Featuring" tracks. It skips this question for albums or playlists.

**Smart File Paths:** If you input a Single Track URL, it will ask if you want to set a specific `.flac` output path. If you do, it intelligently skips all questions about filename formatting and subfolder organization.

**Unified Quality Profiles:** Automatically translates your desired quality tier across different services (like Tidal and Qobuz).

**CLI Generator:** At the end of the configuration, it generates and prints the exact CLI command for your specific setup, so you can copy and reuse it in your automated scripts.

**Profile Save:** After confirming the download, you can save the entire configuration as a named profile to reuse later.

---

## Docker Usage

A CLI-focused Docker image is available for running SpotiFLAC without the desktop GUI.

Build the image:
```bash
docker build -t spotiflac .
```

Run a download with a mounted local output directory:
```bash
docker run --rm -v "$(pwd)/downloads:/app/downloads" spotiflac \
  https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT \
  ./downloads -s tidal -q LOSSLESS
```

The Docker image is intended for CLI mode only; it does not launch the webview GUI.

For interactive terminal mode, run with `-it`:
```bash
docker run --rm -it -v "$(pwd)/downloads:/app/downloads" spotiflac \
  --interactive
```

Published image (GHCR)

Official images are published to GitHub Container Registry (GHCR) via GitHub Actions. You can pull them with:

```bash
docker pull ghcr.io/ShuShuzinhuu/SpotiFLAC-Module-Version:latest
```

Run a pulled image:

```bash
docker run --rm -v "$(pwd)/downloads:/app/downloads" ghcr.io/ShuShuzinhuu/SpotiFLAC-Module-Version:latest \
    https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT ./downloads -s tidal -q LOSSLESS
```

---

```python
from SpotiFLAC import SpotiFLAC

# Simple Download
SpotiFLAC(
    url="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
    output_dir="./downloads"
)
```

CLI usage:
```bash
spotiflac url ./out --service tidal
```

---

### Supported URL Types

SpotiFLAC supports the following URL formats for **Spotify**, **Tidal**, **Apple Music**, **SoundCloud**, **YouTube** and **Pandora**:

| Type                         | Spotify                         | Tidal                                            | Apple Music                                       | SoundCloud                              | YouTube / YT Music                              | Pandora                                        |
|------------------------------|---------------------------------|--------------------------------------------------|---------------------------------------------------|-----------------------------------------|-------------------------------------------------|------------------------------------------------|
| Track                        | `open.spotify.com/track/...`    | `listen.tidal.com/track/...`                     | `music.apple.com/.../song/...`                    | `soundcloud.com/artist/track-slug`      | `youtube.com/watch?v=...` · `youtu.be/...`      | `pandora.com/artist/.../song/TR:...` · `pandora.app.link/...` |
| Album / Set                  | `open.spotify.com/album/...`    | `listen.tidal.com/album/...`                     | `music.apple.com/.../album/...`                   | `soundcloud.com/artist/sets/set-slug`   | `music.youtube.com/playlist?list=OLAK5uy_...`   | —                                              |
| Playlist                     | `open.spotify.com/playlist/...` | `listen.tidal.com/playlist/...`                  | `music.apple.com/.../playlist/...`                | —                                       | `youtube.com/playlist?list=PL...`               | —                                              |
| Discography (via artist URL) | `open.spotify.com/artist/...`   | `listen.tidal.com/artist/.../discography/albums` | `music.apple.com/.../artist/...`                  | —                                       | —                                               | —                                              |

> **Note:** SoundCloud and YouTube tracks are downloaded as **MP3** (neither platform distributes lossless audio). Apple Music downloads as **M4A/ALAC** (lossless) or **AAC** depending on the selected quality. Pandora downloads as **MP3** (mp3_192 by default) or **M4A** (aac_64 / aac_32). All other services deliver **FLAC**.
>
> Joox, NetEase, Migu and Kuwo are **download-only services** — they cannot be used as input URL sources. Use a Spotify or Tidal link and set one of these as the service. These providers are primarily available in select Asian markets and may require a VPN outside those regions.
>
> SoundCloud short links (`on.soundcloud.com/...`) and mobile links (`m.soundcloud.com/...`) are automatically resolved. Tracking parameters (e.g. `?utm_source=...`) are stripped before processing.
>
> Apple Music track links with an `?i=` song parameter (e.g. `music.apple.com/us/album/album-name/id?i=trackid`) are also supported.
>
> Pandora app links (`pandora.app.link/...`) are automatically resolved to their canonical web URL. Pandora pretty URLs (e.g. `pandora.com/artist/artist-name/album-name/song-name/TR:...`) are fully supported.

---

## Advanced Configuration

You can customize the download behavior, prioritize specific streaming services, and organize your files automatically into folders.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ",
    output_dir="./MusicLibrary",
    services=["qobuz", "amazon", "tidal"],
    filename_format="{year} - {album}/{track}. {title}",
    use_artist_subfolders=True,
    use_album_subfolders=True,
    loop=60,                     # retry duration in minutes
    track_max_retries=2,         # extra per-track retries on failure
    post_download_action="notify"
)
```

---

## Service Health Check

SpotiFLAC can probe all provider endpoints before downloading to verify which ones are currently reachable.

In **Interactive Mode** this runs automatically at startup. In code or scripts you can call it directly:

```python
from SpotiFLAC.core.health_check import (
    run_health_check,
    print_health_report,
    get_working_providers,
)

results = run_health_check(["tidal", "qobuz", "deezer", "soundcloud", "pandora"])
print_health_report(results)

working = get_working_providers(results)
print("Available providers:", working)
```

```bash
# CLI: check all services then download
spotiflac https://open.spotify.com/track/... ./out --service tidal qobuz
```

The health check runs **in parallel** with a configurable timeout (default: 5 s per endpoint) and never blocks your download if a check fails. In the GUI, the check reports provider-level availability and endpoint counts, without exposing individual raw endpoint URLs.

---

## Configuration Profiles

Save and reuse complete download configurations without re-typing them every time.

### Save a profile

```bash
# Save current flags as "hires-tidal"
spotiflac https://... ./out \
  --service tidal \
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
        "services":             ["tidal"],
        "quality":              "HI_RES_LOSSLESS",
        "use_album_subfolders": True,
        "filename_format":      "{year} - {album}/{track}. {title}",
    })

    cfg = await get_profile_async("hires-tidal")
    print(await list_profiles_async())  # ['hires-tidal']

asyncio.run(main())
```

Profiles are stored at `~/.cache/spotiflac/profiles.json`. In the Interactive Wizard, you are prompted to load a profile at startup and optionally save one at the end.

---

## Batch Downloads

Pass a list of URLs to download them all in sequence. Failed tracks per URL are collected and can be retried with `loop`.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url=[
        "https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://listen.tidal.com/album/364272512",
    ],
    output_dir="./MusicLibrary",
    services=["tidal", "qobuz"],
    use_album_subfolders=True,
)
```

---

## Auto-Retry on Failure

Set `track_max_retries` (Python) or `--retries` (CLI) to automatically retry failed tracks.
Each retry cycles through **all configured providers** from the beginning, waiting exponentially longer between attempts (2 s → 4 s → 8 s …, capped at 30 s).

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["tidal", "qobuz", "deezer"],
    track_max_retries=3,   # up to 3 extra attempts per track
)
```

```bash
spotiflac https://open.spotify.com/album/... ./out \
  --service tidal qobuz deezer \
  --retries 3
```

> **Tip:** Combine `--retries` with `--loop` for maximum resilience — `--retries` handles transient errors on individual tracks, while `--loop` re-queues permanently failed tracks after N minutes.

---
 
## Per-Track Timeout

Set `timeout_s` (Python) or `--timeout` (CLI) to cap the time SpotiFLAC will spend downloading a single track. If the download does not complete within the specified number of seconds, the process is terminated and the track is marked as failed — allowing the next provider or retry to take over.

```bash
# CLI — skip any track that takes more than 3 minutes
spotiflac https://open.spotify.com/album/... ./out --service tidal --timeout 180
 
# Python API
from SpotiFLAC import SpotiFLAC
SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["tidal", "qobuz"],
    timeout_s=120,
)
```

> **Tip:** Pair `--timeout` with `--retries` so that a stalled track is automatically re-attempted against the next provider instead of blocking the entire queue indefinitely.

---

## Post-Download Actions

| Action        | Description                                                                       |
|---------------|-----------------------------------------------------------------------------------|
| `none`        | Do nothing (default)                                                              |
| `open_folder` | Open the output folder in the system file manager                                 |
| `notify`      | Send an OS desktop notification with a summary                                    |
| `command`     | Run a custom shell command — placeholders: `{folder}`, `{succeeded}`, `{failed}` |

```python
SpotiFLAC(url="...", output_dir="./downloads", post_download_action="open_folder")

SpotiFLAC(url="...", output_dir="./downloads",
          post_download_action="command",
          post_download_command="rsync -av {folder}/ user@nas:/music/")
```

```bash
spotiflac https://... ./out --post-action notify
spotiflac https://... ./out --post-action command --post-command "rsync -av {folder}/ user@nas:/music/"
```

---

## Discography Download

Download the complete discography of an artist. Duplicate tracks (same ISRC across different releases) are automatically skipped.

```python
from SpotiFLAC import SpotiFLAC

# Spotify — albums + singles
SpotiFLAC(url="https://open.spotify.com/artist/1Xyo4u8uXC1ZmMpatF05PJ", output_dir="./MusicLibrary",
          services=["qobuz", "tidal"], use_album_subfolders=True, filename_format="{year} - {album}/{track}. {title}")

# Tidal — full discography (append /discography/albums or /discography/singles to filter)
SpotiFLAC(url="https://listen.tidal.com/artist/7804", output_dir="./MusicLibrary",
          services=["tidal"], use_album_subfolders=True, filename_format="{year} - {album}/{track}. {title}")
```

```bash
spotiflac https://open.spotify.com/artist/... ./MusicLibrary \
  --service tidal --include-featuring \
  --use-album-subfolders --filename-format "{year} - {album}/{track}. {title}"
```

> Recommended layout: `--use-album-subfolders` + `--filename-format "{year} - {album}/{track}. {title}"`.

---

## Custom Output Path (single tracks)

For single track downloads you can specify the **exact file path** instead of relying on `output_dir` + `filename_format`.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
    output_dir="./downloads",
    output_path="files/song.flac"
)
```

> **Note:** `output_path` is automatically ignored when the URL points to an **album**, **playlist**, or **artist/discography**.

---

## Qobuz Local API URL (Optional)

SpotiFLAC can use an optional self-hosted Qobuz stream API for improved reliability and reduced rate limits. If you do not provide a local API URL, Qobuz requests are attempted anonymously.

> **How to deploy your own instance:** [github.com/BartolomeoRusso9/qobuz-rest-api](https://github.com/BartolomeoRusso9/qobuz-rest-api)

### How to Apply Qobuz Local API URL in SpotiFLAC

**Interactive Wizard:** The wizard prompts you to enter your local Qobuz API URL during configuration.

**Environment Variable:**

```bash
export QOBUZ_LOCAL_API_URL="https://localhost:8000"
```

**Python:**
```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="URL",
    output_dir="./downloads",
    qobuz_local_api_url="https://localhost:8000",
)
```

**config.json:**
```json
{
    "qobuz_local_api_url": "https://localhost:8000"
}
```

---

## Custom Tidal API Instance

SpotiFLAC connects to a shared pool of public hifi-api mirrors to fetch Tidal streams.
If you want guaranteed availability and full control, you can self-host your own instance
and point SpotiFLAC to it — it will always be tried first, before any public mirror.

> **How to deploy your own instance:** [github.com/binimum/hifi-api](https://github.com/binimum/hifi-api)



### Python

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
    output_dir="./downloads",
    services=["tidal"],
    tidal_custom_api="https://your-instance.example.com",
)
```

### CLI

```bash
spotiflac https://open.spotify.com/track/... ./downloads \
  --service tidal \
  --tidal-api "https://your-instance.example.com"
```

### Interactive Wizard

The wizard prompts for a custom Tidal API URL at step **12.5**, right after the optional tokens section.

### config.json

```json
{
    "tidal_custom_api": "https://your-instance.example.com"
}
```

> **Note:** The custom instance is also saved and restored when using `--save-profile` / `--profile`.

---

<h2>CLI program usage</h2>
<p>Program can be downloaded for <b>Windows</b>, <b>Linux (x86 and ARM)</b> and <b>MacOS</b>. The downloads are available under the releases.<br>
Program can also be ran by downloading the python files and calling <code>python launcher.py</code> with the arguments.</p>

<h4>Windows example usage:</h4>

```bash
./SpotiFLAC-Windows.exe url
                        output_dir
                        [--service tidal qobuz deezer amazon soundcloud youtube apple pandora joox netease migu kuwo]
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

<h4>Linux / Mac example usage:</h4>

```bash
chmod +x SpotiFLAC-Linux-arm64
./SpotiFLAC-Linux-arm64 url
                        output_dir
                        [--service tidal qobuz deezer amazon soundcloud youtube apple pandora joox netease migu kuwo]
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

---

## API Reference

### `SpotiFLAC()` Parameters

| Parameter                      | Type    | Default                                                   | Description                                                                                                                                                                                                                                                                  |
|--------------------------------|---------|-----------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **`url`**                      | `str` / `list[str]` | *Required*                                   | A single URL or a **list of URLs** (batch mode) for Spotify, Tidal, Apple Music, SoundCloud, YouTube or Pandora.                                                                                                                                                             |
| **`output_dir`**               | `str`   | *Required*                                                | The destination directory path where the audio files will be saved.                                                                                                                                                                                                          |
| **`output_path`**              | `str`   | `None`                                                    | Exact destination file path for **single track** downloads. Overrides `output_dir` + `filename_format`. Automatically ignored for albums, playlists and artist discographies.                                                                                                |
| **`services`**                 | `list`  | `["tidal"]`                                               | Specifies which services to use and their priority order. Choices: `tidal`, `qobuz`, `deezer`, `amazon`, `soundcloud`, `youtube`, `apple`, `pandora`, `joox`, `netease`, `migu`, `kuwo`.                                                                            |
| **`filename_format`**          | `str`   | `"{title} - {artist}"`                                    | Format for naming downloaded files. See placeholders below.                                                                                                                                                                                                                  |
| **`use_track_numbers`**        | `bool`  | `False`                                                   | Prefixes the filename with the track number.                                                                                                                                                                                                                                 |
| **`use_album_track_numbers`**  | `bool`  | `False`                                                   | Uses the track's original album number instead of the download queue position.                                                                                                                                                                                               |
| **`use_artist_subfolders`**    | `bool`  | `False`                                                   | Automatically organizes downloaded files into subfolders by artist.                                                                                                                                                                                                          |
| **`use_album_subfolders`**     | `bool`  | `False`                                                   | Automatically organizes downloaded files into subfolders by album.                                                                                                                                                                                                           |
| **`first_artist_only`**        | `bool`  | `False`                                                   | Uses only the first artist in tags and filename.                                                                                                                                                                                                                             |
| **`include_featuring`**        | `bool`  | `False`                                                   | When downloading an artist discography, also includes tracks where the artist appears as a featured artist.                                                                                                                                                                  |
| **`tidal_custom_api`**         | `str`   | `None`                                                    | URL of a self-hosted [hifi-api](https://github.com/binimum/hifi-api) instance. Takes priority over all public mirrors.                                                                                                                                                       |
| **`timeout_s`**                | `int`   | `None`                                                    | Per-track download timeout in **seconds**. If a single track download does not complete within this time, the process is terminated and the track is marked as failed. SpotiFLAC then moves on to the next provider or retry. Set to `None` (default) to disable the timeout. |
| **`loop`**                     | `int`   | `None`                                                    | Duration in minutes to keep retrying **permanently failed** tracks after a full session completes.                                                                                                                                                                           |
| **`track_max_retries`**        | `int`   | `0`                                                       | Extra download attempts **per track** when all providers fail on the first try. Each retry cycles through all providers again with exponential backoff (2 s → 4 s → 8 s …, capped at 30 s).                                                                                  |
| **`quality`**                  | `str`   | `"LOSSLESS"`                                              | Download quality. Tidal: `"DOLBY_ATMOS"`, `"HI_RES_LOSSLESS"`, `"LOSSLESS"`, `"HIGH"`, `"LOW"`. Qobuz: `"6"` (CD), `"7"` (Hi-Res), `"27"` (Hi-Res Max). Apple Music: `"alac"`, `"atmos"`, `"ac3"`, `"aac"`, `"aac-legacy"`. Pandora: `"mp3_192"`, `"aac_64"`, `"aac_32"`.    |
| **`allow_fallback`**           | `bool`  | `True`                                                    | Automatically falls back to the next available quality tier if the requested quality is unavailable.                                                                                                                                                                         |
| **`log_level`**                | `int`   | `logging.WARNING`                                         | Python logging level.                                                                                                                                                                                                                                                        |
| **`embed_lyrics`**             | `bool`  | `True`                                                    | Whether to fetch and embed synchronized lyrics (LRC) into the audio file.                                                                                                                                                                                                    |
| **`lyrics_providers`**         | `list`  | `["spotify", "apple", "musixmatch", "lrclib", "amazon"]`  | Priority order of lyrics providers to attempt.                                                                                                                                                                                                                               |
| **`enrich_metadata`**          | `bool`  | `True`                                                    | Enables multi-provider metadata enrichment (HD covers, BPM, Labels, etc.).                                                                                                                                                                                                   |
| **`enrich_providers`**         | `list`  | `["deezer", "apple", "qobuz", "tidal", "soundcloud"]`     | Priority order of metadata providers to attempt.                                                                                                                                                                                                                             |
| **`qobuz_local_api_url`**      | `str`   | `None`                                                    | Optional local Qobuz stream API URL. When set, the provider uses this endpoint for Qobuz stream requests.                                                                                                                                                                    |
| **`post_download_action`**     | `str`   | `"none"`                                                  | Action after all downloads finish: `"none"`, `"open_folder"`, `"notify"`, `"command"`.                                                                                                                                                                                       |
| **`post_download_command`**    | `str`   | `""`                                                      | Shell command to run when `post_download_action="command"`. Supports `{folder}`, `{succeeded}`, `{failed}` placeholders.                                                                                                                                                     |

### Filename Format Placeholders

When customizing the `filename_format` string, you can use the following dynamic tags:

* `{title}` — Track title
* `{artist}` — Track artist(s)
* `{album}` — Album name
* `{album_artist}` — The artist(s) of the entire album
* `{disc}` — The disc number
* `{track}` — The track's original number in the album
* `{position}` — Download queue / Playlist position (zero-padded, e.g. `01`)
* `{date}` — Full release date (e.g., YYYY-MM-DD)
* `{year}` — Release year (e.g., YYYY)
* `{isrc}` — Track ISRC code

---

## CLI Flag Reference

| Flag                        | Short | Default                                       | Description                                                                                                                                                                                                 |
|-----------------------------|-------|-----------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--service`                 | `-s`  | `tidal`                                       | One or more providers in priority order. Choices: `tidal`, `qobuz`, `deezer`, `amazon`, `soundcloud`, `youtube`, `apple`, `pandora`, `joox`, `netease`, `migu`, `kuwo`.                                                                            |
| `--filename-format`         | `-f`  | `{title} - {artist}`                          | Filename template with placeholders.                                                                                                                                                                        |
| `--output-path`             | `-o`  | `None`                                        | Exact output file path for single track downloads. Ignored for albums, playlists and discographies.                                                                                                         |
| `--quality`                 | `-q`  | `LOSSLESS`                                    | Audio quality. Tidal: `DOLBY_ATMOS`, `HI_RES_LOSSLESS`, `LOSSLESS`, `HIGH`, `LOW`. Qobuz: `6`, `7`, `27`. Apple Music: `alac`, `atmos`, `ac3`, `aac`, `aac-legacy`. Pandora: `mp3_192`, `aac_64`, `aac_32`. |
| `--use-track-numbers`       |       | `False`                                       | Prefix filenames with track numbers.                                                                                                                                                                        |
| `--use-album-track-numbers` |       | `False`                                       | Use the track's original album number instead of queue position.                                                                                                                                            |
| `--use-artist-subfolders`   |       | `False`                                       | Organize files into per-artist subfolders.                                                                                                                                                                  |
| `--use-album-subfolders`    |       | `False`                                       | Organize files into per-album subfolders.                                                                                                                                                                   |
| `--first-artist-only`       |       | `False`                                       | Use only the first artist in tags and filename.                                                                                                                                                             |
| `--include-featuring`       |       | `False`                                       | Include tracks where the artist appears as a featured artist. Only applies to artist/discography URLs.                                                                                                      |
| `--qobuz-local-api`         |       | `None`                                        | Optional local Qobuz stream API URL.                                                                                                                                                                         |
| `--tidal-api`               |       | `None`                                        | URL of a self-hosted [hifi-api](https://github.com/binimum/hifi-api) instance. Takes priority over the built-in public mirror pool.                                                                         |
| `--timeout`                 |       | `None`                                        | Per-track download timeout in **seconds**. If a track download stalls or takes longer than this limit, it is forcibly terminated and marked as failed, then SpotiFLAC moves to the next provider or retry. |
| `--loop`                    | `-l`  | `None`                                        | Keep retrying permanently failed tracks every N minutes.                                                                                                                                                    |
| `--retries`                 |       | `0`                                           | Extra per-track download attempts on failure. Cycles through all providers with exponential backoff.                                                                                                        |
| `--verbose`                 | `-v`  | `False`                                       | Enable debug logging.                                                                                                                                                                                       |
| `--no-lyrics`               |       | `False`                                       | Disable lyrics embedding (lyrics are embedded **by default**).                                                                                                                                              |
| `--lyrics-providers`        |       | `spotify apple musixmatch lrclib amazon`      | Lyrics provider priority order.                                                                                                                                                                             |
| `--no-enrich`               |       | `False`                                       | Disable multi-provider metadata enrichment (enrichment is **enabled by default**).                                                                                                                          |
| `--enrich-providers`        |       | `deezer apple qobuz tidal soundcloud`         | Metadata enrichment provider priority order.                                                                                                                                                                |
| `--post-action`             |       | `none`                                        | Action after all downloads finish: `none`, `open_folder`, `notify`, `command`.                                                                                                                              |
| `--post-command`            |       | `""`                                          | Shell command for `--post-action=command`. Placeholders: `{folder}`, `{succeeded}`, `{failed}`.                                                                                                             |
| `--profile`                 |       | `None`                                        | Load a saved profile. CLI flags override profile values.                                                                                                                                                    |
| `--save-profile`            |       | `None`                                        | Save current CLI configuration as a named profile after the run.                                                                                                                                            |

---

## MusicBrainz Enrichment

SpotiFLAC automatically queries **MusicBrainz** in the background (when an ISRC is available) while the audio is being downloaded, adding professional-grade tags at no extra time cost. Fields written when found:

| Tag                             | Description                               |
|---------------------------------|-------------------------------------------|
| `GENRE`                         | Genre(s), sorted by popularity (up to 5)  |
| `BPM`                           | Beats per minute                          |
| `LABEL` / `ORGANIZATION`        | Record label name                         |
| `CATALOGNUMBER`                 | Catalog number                            |
| `BARCODE`                       | Release barcode / UPC                     |
| `ORIGINALDATE` / `ORIGINALYEAR` | First-ever release date                   |
| `RELEASECOUNTRY`                | Country of release                        |
| `RELEASESTATUS`                 | Release status (e.g. `Official`)          |
| `RELEASETYPE`                   | Release type (e.g. `Album`, `Single`)     |
| `MEDIA`                         | Media format (e.g. `CD`, `Digital Media`) |
| `SCRIPT`                        | Script of the release text                |
| `ARTISTSORT`                    | Artist sort name for file managers        |
| `MUSICBRAINZ_TRACKID`           | MusicBrainz recording ID                  |
| `MUSICBRAINZ_ALBUMID`           | MusicBrainz release ID                    |
| `MUSICBRAINZ_ARTISTID`          | MusicBrainz artist ID                     |
| `MUSICBRAINZ_RELEASEGROUPID`    | MusicBrainz release group ID              |
| `MUSICBRAINZ_ALBUMARTISTID`     | MusicBrainz album artist ID               |
| `ALBUMARTISTSORT`               | Album artist sort name for file managers  |

---

## Download Validation

After each download, SpotiFLAC validates the file to detect common issues:

- **Preview detection** — if the expected duration is ≥ 60 s but the downloaded file is ≤ 35 s, the file is deleted and the download is retried with the next provider.
- **Duration mismatch** — for tracks longer than 90 s, a deviation greater than 25% (or 15 s minimum) from the expected duration is treated as a corrupt download and the file is removed.

---

### Want to support the project?

_If this software is useful and brings you value,
consider supporting the project by buying us a coffee.
Your support helps keep development going._

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/shukurenais)
[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/bartolomeorusso9)

## API Credits

[Song.link](https://song.link) · [hifi-api](https://github.com/binimum/hifi-api) · [qobuz-rest-api](https://github.com/BartolomeoRusso9/qobuz-rest-api) ·[dabmusic.xyz](https://dabmusic.xyz) · [GD Studio Music API](https://music.gdstudio.xyz) · [Music Wjhe API](https://music.wjhe.top/) · [afkarxyz](https://github.com/afkarxyz) · [MusicBrainz](https://musicbrainz.org) · [SoundCloud](https://soundcloud.com) · [Apple Music](https://music.apple.com) · [YouTube Music](https://music.youtube.com) · [Pandora](https://www.pandora.com) · [squid.wtf](https://squid.wtf) · [flacdownloader.com](https://flacdownloader.com)

> [!TIP]
>
> **Star Us**, You will receive all release notifications from GitHub without any delay
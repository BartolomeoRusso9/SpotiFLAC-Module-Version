# SpotiFLAC Python Module

[![PyPI - Version](https://img.shields.io/pypi/v/spotiflac?style=for-the-badge&logo=pypi&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/) [![PyPI - Python Version](https://img.shields.io/pypi/pyversions/spotiflac?style=for-the-badge&logo=python&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/) [![Pepy Total Downloads](https://img.shields.io/pepy/dt/spotiflac?style=for-the-badge&logo=pypi&logoColor=ffffff&labelColor=000000)](https://pypi.org/project/SpotiFLAC/)


Integrate **SpotiFLAC** directly into your Python projects. Perfect for building custom Telegram bots, automation tools, bulk downloaders, downloading music for Jellyfin or web interfaces.

> **Looking for a standalone app?**
### [SpotiFLAC (Desktop)](https://github.com/afkarxyz/SpotiFLAC)

Download music in true lossless FLAC from Tidal, Qobuz & Amazon Music for Windows, macOS & Linux

### [SpotiFLAC (Mobile)](https://github.com/zarzet/SpotiFLAC-Mobile)

SpotiFLAC for Android & iOS — maintained by [@zarzet](https://github.com/zarzet)

---

## Installation

```bash
pip install SpotiFLAC
```

---

## Quick Start

The easiest way to use SpotiFLAC is through the built-in Interactive Wizard. Just run the command without any arguments:
```bash
SpotiFLAC
```
> (Or python launcher.py if running from source)

---
## Interactive Mode

SpotiFLAC features a smart Interactive Mode that guides you step-by-step. It dynamically adjusts its questions based on your inputs:

* **Smart URL Detection:** If you input an Artist URL, it will ask if you want to download "Featuring" tracks. It skips this question for albums or playlists.
* **Smart File Paths:** If you input a Single Track URL, it will ask if you want to set a specific `.flac` output path. If you do, it intelligently skips all questions about filename formatting and subfolder organization.
* **Unified Quality Profiles:** Automatically translates your desired quality tier across different services (like Tidal and Qobuz).
* **CLI Generator:** At the end of the configuration, it generates and prints the exact CLI command for your specific setup, so you can copy and reuse it in your automated scripts.
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

## Supported URL Types

SpotiFLAC supports the following URL formats for **Spotify**, **Tidal** and **SoundCloud**:

| Type                          | Spotify                         | Tidal                                            | SoundCloud                              |
|-------------------------------|---------------------------------|--------------------------------------------------|-----------------------------------------|
| Track                         | `open.spotify.com/track/...`    | `listen.tidal.com/track/...`                     | `soundcloud.com/artist/track-slug`      |
| Album / Set                   | `open.spotify.com/album/...`    | `listen.tidal.com/album/...`                     | `soundcloud.com/artist/sets/set-slug`   |
| Playlist                      | `open.spotify.com/playlist/...` | `listen.tidal.com/playlist/...`                  | —                                       |
| Discography (via artist URL)  | `open.spotify.com/artist/...`   | `listen.tidal.com/artist/.../discography/albums` | —                                       |

> **Note:** SoundCloud tracks are downloaded as **MP3** (the platform does not distribute lossless audio). All other services deliver **FLAC**.

---

## Advanced Configuration

You can customize the download behavior, prioritize specific streaming services, and organize your files automatically into folders.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ",
    output_dir="./MusicLibrary",
    services=["qobuz", "amazon", "tidal", "spoti"],
    filename_format="{year} - {album}/{track}. {title}",
    use_artist_subfolders=True,
    use_album_subfolders=True,
    loop=60 # Retry duration in minutes
)
```

---

## Discography Download

Download the complete discography of an artist — both from Spotify and Tidal. Duplicate tracks (same ISRC across different releases) are automatically skipped.

### Spotify

```python
from SpotiFLAC import SpotiFLAC

# Full discography (albums + singles)
SpotiFLAC(
    url="https://open.spotify.com/artist/1Xyo4u8uXC1ZmMpatF05PJ",
    output_dir="./MusicLibrary",
    services=["qobuz", "tidal"],
    use_album_subfolders=True,
    filename_format="{year} - {album}/{track}. {title}",
)
```

### Tidal

```python
from SpotiFLAC import SpotiFLAC

# Full discography
SpotiFLAC(
    url="https://listen.tidal.com/artist/7804",
    output_dir="./MusicLibrary",
    services=["tidal"],
    use_album_subfolders=True,
    filename_format="{year} - {album}/{track}. {title}",
)

# Albums only
SpotiFLAC(
    url="https://listen.tidal.com/artist/7804/discography/albums",
    output_dir="./MusicLibrary",
    services=["tidal"],
)

# Singles only
SpotiFLAC(
    url="https://listen.tidal.com/artist/7804/discography/singles",
    output_dir="./MusicLibrary",
    services=["tidal"],
)
```

### CLI

```bash
# Spotify artist — albums, singles + featuring tracks
spotiflac https://open.spotify.com/artist/... ./MusicLibrary \
  --service tidal \
  --include-featuring \
  --use-album-subfolders \
  --filename-format "{year} - {album}/{track}. {title}"
  
# Spotify artist (albums + singles)
spotiflac https://open.spotify.com/artist/... ./MusicLibrary \
  --service qobuz tidal \
  --use-album-subfolders \
  --filename-format "{year} - {album}/{track}. {title}"

# Tidal artist — albums, singles + compilations (featuring tracks)
spotiflac https://listen.tidal.com/artist/7804 ./MusicLibrary \
  --service tidal \
  --include-featuring

# Tidal artist (album + singles)
spotiflac https://listen.tidal.com/artist/7804 ./MusicLibrary \
  --service tidal \
  --use-album-subfolders \
  --filename-format "{year} - {album}/{track}. {title}"

# Tidal — albums only
spotiflac https://listen.tidal.com/artist/7804/discography/albums ./MusicLibrary \
  --service tidal
```

### Recommended folder structure for discographies

```
MusicLibrary/
  Artist Name/
    2019 - Album Title/
      01. Song One.flac
      02. Song Two.flac
    2023 - Single Title/
      01. Song Title.flac
```

Use `--use-album-subfolders` and `--filename-format "{year} - {album}/{track}. {title}"` to achieve this layout automatically.

---

## SoundCloud Download

SpotiFLAC can download tracks and sets directly from SoundCloud. The output format is **MP3** (SoundCloud does not offer lossless streams). Metadata enrichment and lyrics embedding are fully supported.

```python
from SpotiFLAC import SpotiFLAC

# Single track
SpotiFLAC(
    url="https://soundcloud.com/artist/track-slug",
    output_dir="./downloads",
    services=["soundcloud"],
    embed_lyrics=True,
    enrich_metadata=True,
)
```

CLI equivalent:

```bash
spotiflac https://soundcloud.com/artist/track-slug ./downloads \
  --service soundcloud \
  --enrich-providers deezer apple qobuz tidal soundcloud
```

> Metadata enrichment via Deezer, Apple Music and others still applies to SoundCloud tracks — BPM, genre, label and HD artwork are fetched from those providers using the track's ISRC when available.

---

## Custom Output Path (single tracks)

For single track downloads you can specify the **exact file path** instead of relying on `output_dir` + `filename_format`. This is useful when you need full control over the filename from an external script, a Telegram bot, or any automation tool.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
    output_dir="./downloads",          # fallback if output_path is not set
    output_path="files/song.flac"      # exact destination path
)
```

CLI equivalent:

```bash
spotiflac https://open.spotify.com/track/... ./downloads --output-path files/song.flac
```

> **Note:** `output_path` is automatically ignored when the URL points to an **album**, **playlist**, or **artist/discography** — a warning will be printed and files will be saved normally into `output_dir`. Non-existent parent directories are created automatically.

---

## Qobuz Token (Optional)

Setting a personal Qobuz token improves metadata resolution reliability. The token is used as a **last resort fallback** — requests are first attempted anonymously, and only if they fail (HTTP 400/401) the token is injected. A **free Qobuz account** is sufficient.

> **Important:** Use throwaway credentials (random email + password you won't forget). You'll need them again if the token expires and needs to be regenerated.

### How to Create a Free Account

Go to [qobuz.com](https://www.qobuz.com) and register. No payment method required for the free tier.

### How to Extract Your Token

1. Log in to [play.qobuz.com](https://play.qobuz.com)
2. Open DevTools with **F12** → go to the **Network** tab
3. Play any track or perform any search to trigger API calls
4. Filter requests by typing `api.json` in the search bar
5. Click on any request to `www.qobuz.com/api.json/...`
6. In the **Request Headers** panel, look for: **x-user-auth-token: your_token_here**
7. Copy the value — that is your token

---

## Spotify Token (sp_dc) for Synced Lyrics (Optional)
Spotify requires a session cookie called sp_dc to access its internal synced lyrics API.

### How to Extract Your Token
1. Open your web browser and go to open.spotify.com
2. Log in to your Spotify account.
3. Open DevTools (F12 or Ctrl+Shift+I / Cmd+Option+I).
4. Navigate to the Application tab (or "Storage" in Firefox).
5. On the left sidebar, expand Cookies and select https://open.spotify.com.
6. Search for the row named sp_dc.
7. Double-click its Value, copy it, and keep it safe. (Do not share this token!)

## How to Apply Tokens in SpotiFLAC
Once you have your Qobuz or Spotify tokens, you can pass them to SpotiFLAC in several ways:

### Interactive Wizard (Easiest)
Simply run `spotiflac` in your terminal. The wizard will prompt you to paste your **sp_dc cookie** or **Qobuz token** during the final configuration steps.

### Environment Variable (all platforms)

The recommended approach across all systems:

```bash
export QOBUZ_AUTH_TOKEN="YOUR_TOKEN_HERE"
export SPOTIFY_TOKEN="YOUR_SP_DC_COOKIE"
```
### On Windows (Command Prompt):
```bash
set QOBUZ_AUTH_TOKEN="YOUR_TOKEN_HERE"
set SPOTIFY_TOKEN="YOUR_SP_DC_COOKIE"
```
### On Windows (PowerShell):
```bash
$env:QOBUZ_AUTH_TOKEN="YOUR_TOKEN_HERE"
$env:SPOTIFY_TOKEN="YOUR_SP_DC_COOKIE"
```
> To make it permanent on Linux/macOS, add the export line to your **~/.bashrc, ~/.zshrc**, or equivalent shell config file.


### .env file (Environment Variables)

If you prefer using a local configuration file for environment variables (highly recommended for development or Docker), you can create a file named .env in the root folder of your project:
```env
QOBUZ_AUTH_TOKEN=YOUR_QOBUZ_TOKEN
SPOTIFY_TOKEN=YOUR_SP_DC_COOKIE
```

You can load this file before running the script from the terminal:
```bash
export $(cat .env | xargs) && python launcher.py "URL" ./downloads
```

Or, if you use Docker Compose, you can easily integrate it:
```yaml
services:
  spotiflac:
    env_file:
      - .env
```
> Add **.env** to your **.gitignore** to avoid accidentally committing your token.

### CLI (Terminal)
```bash
python launcher.py "URL" ./downloads \
    --qobuz-token "YOUR_QOBUZ_TOKEN" \
    --spotify-token "YOUR_SP_DC_COOKIE"
```

### Python
```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="URL",
    output_dir="./downloads",
    qobuz_token="YOUR_QOBUZ_TOKEN",
    lyrics_spotify_token="YOUR_SP_DC_COOKIE",
)
```

### config.json
```json
{
    "qobuz_token": "YOUR_QOBUZ_TOKEN",
    "spotify_token": "YOUR_SP_DC_COOKIE"
}
```

<h2>CLI program usage</h2>
<p>Program can be downloaded for <b>Windows</b>, <b>Linux (x86 and ARM)</b> and <b>MacOS</b>. The downloads are available under the releases.<br>
Program can also be ran by downloading the python files and calling <code>python launcher.py</code> with the arguments.</p>

<h4>Windows example usage:</h4>

```bash
./SpotiFLAC-Windows.exe url
                        output_dir
                        [--service tidal qobuz amazon spoti soundcloud]
                        [--filename-format "{title} - {artist}"]
                        [--output-path "files/song.flac"]
                        [--quality LOSSLESS]
                        [--use-track-numbers]
                        [--use-album-track-numbers]
                        [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--first-artist-only]
                        [--qobuz-token TOKEN]
                        [--loop minutes]
                        [--verbose]
                        [--no-lyrics]
                        [--lyrics-providers spotify apple musixmatch amazon lrclib]
                        [--spotify-token SP_DC]
                        [--no-enrich]
                        [--enrich-providers deezer apple qobuz tidal soundcloud]
```

<h4>Linux / Mac example usage:</h4>

```bash
chmod +x SpotiFLAC-Linux-arm64
./SpotiFLAC-Linux-arm64 url
                        output_dir
                        [--service tidal qobuz amazon spoti soundcloud]
                        [--filename-format "{title} - {artist}"]
                        [--output-path "files/song.flac"]
                        [--quality LOSSLESS]
                        [--use-track-numbers]
                        [--use-album-track-numbers]
                        [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--first-artist-only]
                        [--qobuz-token TOKEN]
                        [--loop minutes]
                        [--verbose]
                        [--no-lyrics]
                        [--lyrics-providers spotify apple musixmatch amazon lrclib]
                        [--spotify-token SP_DC]
                        [--no-enrich]
                        [--enrich-providers deezer apple qobuz tidal soundcloud]
```

*(For ARM devices like Raspberry Pi, replace `x86_64` with `arm64`)*
---

## API Reference

### `SpotiFLAC()` Parameters

| Parameter | Type | Default                                                  | Description |
| --- | --- |----------------------------------------------------------| --- |
| **`url`** | `str` | *Required*                                               | The Spotify, Tidal or SoundCloud URL (Track, Album, Playlist, or Artist) you want to download. |
| **`output_dir`** | `str` | *Required*                                               | The destination directory path where the audio files will be saved. |
| **`output_path`** | `str` | `None`                                                   | Exact destination file path for **single track** downloads (e.g. `"files/song.flac"`). Overrides `output_dir` + `filename_format`. Automatically ignored for albums, playlists and artist discographies. |
| **`services`** | `list` | `["tidal"]`                                              | Specifies which services to use and their priority order. Choices: `tidal`, `qobuz`, `amazon`, `spoti`, `soundcloud`. |
| **`filename_format`** | `str` | `"{title} - {artist}"`                                   | Format for naming downloaded files. See placeholders below. |
| **`use_track_numbers`** | `bool` | `False`                                                  | Prefixes the filename with the track number. |
| **`use_album_track_numbers`** | `bool` | `False`                                                  | Uses the track's original album number instead of the download queue position. |
| **`use_artist_subfolders`** | `bool` | `False`                                                  | Automatically organizes downloaded files into subfolders by artist. |
| **`use_album_subfolders`** | `bool` | `False`                                                  | Automatically organizes downloaded files into subfolders by album. |
| **`first_artist_only`** | `bool` | `False`                                                  | Uses only the first artist in tags and filename (e.g. `"Artist A"` instead of `"Artist A, Artist B"`). |
| **`include_featuring`** | `bool` | `False` | When downloading an artist discography, also includes tracks where the artist appears as a featured artist on other artists' releases (`appears_on` on Spotify, `COMPILATIONS` on Tidal). Only the specific tracks featuring the artist are downloaded, not entire albums. |
| **`loop`** | `int` | `None`                                                   | Duration in minutes to keep retrying failed downloads. |
| **`quality`** | `str` | `"LOSSLESS"`                                             | Download quality. Tidal: `"LOSSLESS"` or `"HI_RES"`. Qobuz: `"6"` (CD), `"7"` (Hi-Res), `"27"` (Hi-Res Max). Not applicable to SoundCloud. |
| **`log_level`** | `int` | `logging.WARNING`                                        | Python logging level (e.g. `logging.DEBUG` for verbose output). |
| **`embed_lyrics`** | `bool` | `True`                                                   | Whether to fetch and embed synchronized lyrics (LRC) into the audio file. Disable with `False`. |
| **`lyrics_providers`** | `list` | `["spotify", "musixmatch", "lrclib", "apple"]`           | Priority order of lyrics providers to attempt. |
| **`lyrics_spotify_token`** | `str` | `""`                                                     | Spotify `sp_dc` cookie required for Spotify lyrics. |
| **`enrich_metadata`** | `bool` | `True`                                                   | Enables multi-provider metadata enrichment (High-res covers, BPM, Labels, etc.). Disable with `False`. |
| **`enrich_providers`** | `list` | `["deezer", "apple", "qobuz", "tidal", "soundcloud"]`   | Priority order of metadata providers to attempt. |
| **`qobuz_token`** | `str` | `None`                                                   | Optional Qobuz user auth token used as fallback for metadata resolution. Fallback: env `QOBUZ_AUTH_TOKEN`. |

### Filename Format Placeholders

When customizing the `filename_format` string, you can use the following dynamic tags:

* `{title}` - Track title
* `{artist}` - Track artist(s)
* `{album}` - Album name
* `{album_artist}` - The artist(s) of the entire album
* `{disc}` - The disc number
* `{track}` - The track's original number in the album
* `{position}` - Download queue / Playlist position (zero-padded, e.g. `01`)
* `{date}` - Full release date (e.g., YYYY-MM-DD)
* `{year}` - Release year (e.g., YYYY)
* `{isrc}` - Track ISRC code

---

## MusicBrainz Enrichment

SpotiFLAC automatically queries **MusicBrainz** in the background (when an ISRC is available) while the audio is being downloaded, adding professional-grade tags at no extra time cost. Fields written when found:

| Tag | Description |
| --- | --- |
| `GENRE` | Genre(s), sorted by popularity (up to 5) |
| `BPM` | Beats per minute |
| `LABEL` / `ORGANIZATION` | Record label name |
| `CATALOGNUMBER` | Catalog number |
| `BARCODE` | Release barcode / UPC |
| `ORIGINALDATE` / `ORIGINALYEAR` | First-ever release date |
| `RELEASECOUNTRY` | Country of release |
| `RELEASESTATUS` | Release status (e.g. `Official`) |
| `RELEASETYPE` | Release type (e.g. `Album`, `Single`) |
| `MEDIA` | Media format (e.g. `CD`, `Digital Media`) |
| `SCRIPT` | Script of the release text |
| `ARTISTSORT` | Artist sort name for file managers |
| `MUSICBRAINZ_TRACKID` | MusicBrainz recording ID |
| `MUSICBRAINZ_ALBUMID` | MusicBrainz release ID |
| `MUSICBRAINZ_ARTISTID` | MusicBrainz artist ID |
| `MUSICBRAINZ_RELEASEGROUPID` | MusicBrainz release group ID |

This is enabled automatically for **Tidal**, **Qobuz**, **Amazon** and **SoundCloud** providers (when ISRC is available). No configuration required.

---

## Download Validation

After each download, SpotiFLAC validates the file to detect common issues:

- **Preview detection** — if the expected duration is ≥ 60 s but the downloaded file is ≤ 35 s, the file is deleted and the download is retried with the next provider.
- **Duration mismatch** — for tracks longer than 90 s, a deviation greater than 25% (or 15 s minimum) from the expected duration is treated as a corrupt download and the file is removed.

---

## CLI Flag Reference

| Flag                        | Short | Default                                  | Description                                                                                                                  |
|-----------------------------| --- |------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| `--service`                 | `-s` | `tidal`                                  | One or more providers in priority order. Choices: `tidal`, `qobuz`, `amazon`, `spoti`, `soundcloud`.                        |
| `--filename-format`         | `-f` | `{title} - {artist}`                     | Filename template with placeholders.                                                                                         |
| `--output-path`             | `-o` | `None`                                   | Exact output file path for single track downloads (e.g. `files/song.flac`). Ignored for albums, playlists and discographies. |
| `--quality`                 | `-q` | `LOSSLESS`                               | Audio quality (see Quality table above). Not applicable to SoundCloud.                                                       |
| `--use-track-numbers`       | | `False`                                  | Prefix filenames with track numbers.                                                                                         |
| `--use-album-track-numbers` | | `False`                                  | Use the track's original album number instead of queue position.                                                             |
| `--use-artist-subfolders`   | | `False`                                  | Organize files into per-artist subfolders.                                                                                   |
| `--use-album-subfolders`    | | `False`                                  | Organize files into per-album subfolders.                                                                                    |
| `--first-artist-only`       | | `False`                                  | Use only the first artist in tags and filename.                                                                              |
| `--include-featuring`       | | `False`                                  | Include tracks where the artist appears as a featured artist on other artists' releases. Only applies to artist/discography URLs.|
| `--qobuz-token`             | | `None`                                   | Qobuz user auth token (`x-user-auth-token`).                                                                                 |
| `--loop`                    | `-l` | `None`                                   | Keep retrying failed downloads for N minutes.                                                                                |
| `--verbose`                 | `-v` | `False`                                  | Enable debug logging.                                                                                                        |
| `--no-lyrics`               | | `False`                                  | Disable lyrics embedding (lyrics are embedded **by default**).                                                               |
| `--lyrics-providers`        | | `spotify musixmatch lrclib apple`        | Lyrics provider priority order.                                                                                              |
| `--spotify-token`           | | `""`                                     | Spotify `sp_dc` cookie for synced lyrics.                                                                                    |
| `--no-enrich`               | | `False`                                  | Disable multi-provider metadata enrichment (enrichment is **enabled by default**).                                           |
| `--enrich-providers`        | | `deezer apple qobuz tidal soundcloud`    | Metadata enrichment provider priority order.                                                                                 |

---

### Want to support the project?

_If this software is useful and brings you value,
consider supporting the project by buying me a coffee.
Your support helps keep development going._

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/shukurenais)

## API Credits

[Song.link](https://song.link) · [hifi-api](https://github.com/binimum/hifi-api) · [dabmusic.xyz](https://dabmusic.xyz) · [afkarxyz](https://github.com/afkarxyz) · [MusicBrainz](https://musicbrainz.org) · [SoundCloud](https://soundcloud.com)

> [!TIP]
>
> **Star Us**, You will receive all release notifications from GitHub without any delay ~
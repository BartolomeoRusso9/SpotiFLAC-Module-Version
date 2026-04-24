# SpotiFLAC Python Module

[![PyPI - Version](https://img.shields.io/pypi/v/spotiflac?style=for-the-badge&logo=pypi&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/) [![PyPI - Python Version](https://img.shields.io/pypi/pyversions/spotiflac?style=for-the-badge&logo=python&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/) [![Pepy Total Downloads](https://img.shields.io/pepy/dt/spotiflac?style=for-the-badge&logo=pypi&logoColor=ffffff&labelColor=000000)](https://pypi.org/project/SpotiFLAC/)


Integrate **SpotiFLAC** directly into your Python projects. Perfect for building custom Telegram bots, automation tools, bulk downloaders, jellyfin downloader musics or web interfaces.

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

Import the module and start downloading immediately:

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
spotiflac url ./out --service tidal --use-artist-subfolders
```

---

## Advanced Configuration

You can customize the download behavior, prioritize specific streaming services, and organize your files automatically into folders.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ",
    output_dir="./MusicLibrary",
    services=["qobuz", "amazon", "tidal", "youtube"],
    filename_format="{year} - {album}/{track}. {title}",
    use_artist_subfolders=True,
    use_album_subfolders=True,
    loop=60 # Retry duration in minutes
)
```


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

## Musixmatch Token (usertoken) for Rich-Synced Lyrics (Optional)
Musixmatch offers highly accurate word-level synchronized lyrics. To use it, you need a user token from their desktop app.

### How to Extract Your Token
1. Download and install the official Musixmatch Desktop App (Windows/Mac).
2. Log in to your account.
3. Open DevTools inside the app (usually Ctrl+Shift+I on Windows or Cmd+Option+I on Mac).
4. Go to **Network** tab.
5. Play a song or view lyrics in the app to generate network traffic.
6. Look at the requests being made and inspect their Headers or Payload.
7. Find the usertoken parameter and copy its alphanumeric value.

## How to Apply Tokens in SpotiFLAC
Once you have your Spotify or Musixmatch tokens, you can pass them to SpotiFLAC in several ways:

### Environment Variable (all platforms)

The recommended approach across all systems:

```bash
export QOBUZ_AUTH_TOKEN="YOUR_TOKEN_HERE"
export SPOTIFY_TOKEN="YOUR_SP_DC_COOKIE"
export MUSIXMATCH_TOKEN="YOUR_USERTOKEN"
```
### On Windows (Command Prompt):
```bash
set QOBUZ_AUTH_TOKEN="YOUR_TOKEN_HERE"
set SPOTIFY_TOKEN="YOUR_SP_DC_COOKIE"
set MUSIXMATCH_TOKEN="YOUR_USERTOKEN"
```
### On Windows (PowerShell):
```bash
$env:QOBUZ_AUTH_TOKEN="YOUR_TOKEN_HERE"
$env:SPOTIFY_TOKEN="YOUR_SP_DC_COOKIE"
$env:MUSIXMATCH_TOKEN="YOUR_USERTOKEN"
```
> To make it permanent on Linux/macOS, add the export line to your **~/.bashrc, ~/.zshrc**, or equivalent shell config file.


### .env file (Environment Variables)

If you prefer using a local configuration file for environment variables (highly recommended for development or Docker), you can create a file named .env in the root folder of your project:
```env
QOBUZ_AUTH_TOKEN=YOUR_QOBUZ_TOKEN
SPOTIFY_TOKEN=YOUR_SP_DC_COOKIE
MUSIXMATCH_TOKEN=YOUR_USERTOKEN
```

You can load this file before running the script from the terminal:
```bash
export $(cat .env | xargs) && python launcher.py "URL" ./downloads --embed-lyrics
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
    --embed-lyrics \
    --qobuz-token "YOUR_QOBUZ_TOKEN" \
    --spotify-token "YOUR_SP_DC_COOKIE" \
    --musixmatch-token "YOUR_MUSIXMATCH_USERTOKEN"
```

### Python
```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="URL",
    output_dir="./downloads",
    embed_lyrics=True,
    qobuz_token="YOUR_QOBUZ_TOKEN",
    lyrics_spotify_token="YOUR_SP_DC_COOKIE",
    lyrics_musixmatch_token="YOUR_MUSIXMATCH_USERTOKEN",
)
```

### config.json
```json
{
    "qobuz_token": "YOUR_QOBUZ_TOKEN",
    "spotify_token": "YOUR_SP_DC_COOKIE",
    "musixmatch_token": "YOUR_MUSIXMATCH_USERTOKEN",
    "embed_lyrics": true
}
```

<h2>CLI program usage</h2>
<p>Program can be downloaded for <b>Windows</b>, <b>Linux (x86 and ARM)</b> and <b>MacOS</b>. The downloads are available under the releases.<br>
Program can also be ran by downloading the python files and calling <code>python launcher.py</code> with the arguments.</p>

<h4>Windows example usage:</h4>

```bash
./SpotiFLAC-Windows.exe url
                        output_dir
                        [--service tidal qobuz deezer amazon youtube]
                        [--filename-format "{title} - {artist}"]
                        [--quality LOSSLESS]
                        [--use-track-numbers]
                        [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--first-artist-only]
                        [--qobuz-token TOKEN]
                        [--loop minutes]
                        [--verbose]
                        [--embed-lyrics]
                        [--lyrics-providers spotify musixmatch amazon lrclib]
                        [--spotify-token SP_DC]
                        [--musixmatch-token TOKEN]
                        [--enrich]
                        [--enrich-providers deezer apple qobuz tidal]
```

<h4>Linux / Mac example usage:</h4>

```bash
chmod +x SpotiFLAC-Linux-arm64
./SpotiFLAC-Linux-arm64 url
                        output_dir
                        [--service tidal qobuz deezer amazon youtube]
                        [--filename-format "{title} - {artist}"]
                        [--quality LOSSLESS]
                        [--use-track-numbers]
                        [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--first-artist-only]
                        [--qobuz-token TOKEN]
                        [--loop minutes]
                        [--verbose]
                        [--embed-lyrics]
                        [--lyrics-providers spotify musixmatch amazon lrclib]
                        [--spotify-token SP_DC]
                        [--musixmatch-token TOKEN]
                        [--enrich]
                        [--enrich-providers deezer apple qobuz tidal]
```

*(For ARM devices like Raspberry Pi, replace `x86_64` with `arm64`)*
---

## API Reference

### `SpotiFLAC()` Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| **`url`** | `str` | *Required* | The Spotify URL (Track, Album, or Playlist) you want to download. |
| **`output_dir`** | `str` | *Required* | The destination directory path where the audio files will be saved. |
| **`services`** | `list` | `["tidal"]` | Specifies which services to use and their priority order. |
| **`filename_format`** | `str` | `"{title} - {artist}"` | Format for naming downloaded files. See placeholders below. |
| **`use_track_numbers`** | `bool` | `False` | Prefixes the filename with the track number. |
| **`use_artist_subfolders`** | `bool` | `False` | Automatically organizes downloaded files into subfolders by artist. |
| **`use_album_subfolders`** | `bool` | `False` | Automatically organizes downloaded files into subfolders by album. |
| **`first_artist_only`** | `bool` | `False` | Uses only the first artist in tags and filename (e.g. `"Artist A"` instead of `"Artist A, Artist B"`). |
| **`loop`** | `int` | `None` | Duration in minutes to keep retrying failed downloads. |
| **`quality`** | `str` | `"LOSSLESS"` | Download quality. Tidal: `"LOSSLESS"` or `"HI_RES"`. Qobuz: `"6"` (CD), `"7"` (Hi-Res), `"27"` (Hi-Res Max). |
| **`log_level`** | `int` | `logging.WARNING` | Python logging level (e.g. `logging.DEBUG` for verbose output). |
| **`embed_lyrics`** | `bool` | `False` | Whether to fetch and embed synchronized lyrics (LRC) into the audio file. |
| **`lyrics_providers`** | `list` | `["spotify", "musixmatch", "amazon", "lrclib"]` | Priority order of lyrics providers to attempt. |
| **`lyrics_spotify_token`** | `str` | `""` | Spotify `sp_dc` cookie required for Spotify lyrics. |
| **`lyrics_musixmatch_token`** | `str` | `""` | Musixmatch `usertoken` required for Musixmatch lyrics. |
| **`enrich_metadata`** | `bool` | `False` | Enables multi-provider metadata enrichment (High-res covers, BPM, Labels, etc.). |
| **`enrich_providers`** | `list` | `["deezer", "apple", "qobuz", "tidal"]` | Priority order of metadata providers to attempt. |
| **`qobuz_token`** | `str` | `None` | Optional Qobuz user auth token used as fallback for metadata resolution. Fallback: env `QOBUZ_AUTH_TOKEN`. |

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

This is enabled automatically for **Tidal** and **Qobuz** providers. No configuration required.

---

## Download Validation

After each download, SpotiFLAC validates the file to detect common issues:

- **Preview detection** — if the expected duration is ≥ 60 s but the downloaded file is ≤ 35 s, the file is deleted and the download is retried with the next provider.
- **Duration mismatch** — for tracks longer than 90 s, a deviation greater than 25% (or 15 s minimum) from the expected duration is treated as a corrupt download and the file is removed.

---

## CLI Flag Reference

| Flag | Short | Default | Description |
| --- | --- | --- | --- |
| `--service` | `-s` | `tidal` | One or more providers in priority order. Choices: `tidal`, `qobuz`, `deezer`, `amazon`, `youtube`. |
| `--filename-format` | `-f` | `{title} - {artist}` | Filename template with placeholders. |
| `--quality` | `-q` | `LOSSLESS` | Audio quality (see Quality table above). |
| `--use-track-numbers` | | `False` | Prefix filenames with track numbers. |
| `--use-artist-subfolders` | | `False` | Organise files into per-artist subfolders. |
| `--use-album-subfolders` | | `False` | Organise files into per-album subfolders. |
| `--first-artist-only` | | `False` | Use only the first artist in tags and filename. |
| `--qobuz-token` | | `None` | Qobuz user auth token (`x-user-auth-token`). |
| `--loop` | `-l` | `None` | Repeat every N minutes. |
| `--verbose` | `-v` | `False` | Enable debug logging. |
| `--embed-lyrics` | | `False` | Fetch and embed synced lyrics into the file. |
| `--lyrics-providers` | | `spotify musixmatch amazon lrclib` | Lyrics provider priority order. |
| `--spotify-token` | | `""` | Spotify `sp_dc` cookie for synced lyrics. |
| `--musixmatch-token` | | `""` | Musixmatch desktop usertoken. |
| `--enrich` | | `False` | Enable multi-provider metadata enrichment. |
| `--enrich-providers` | | `deezer apple qobuz tidal` | Metadata enrichment provider priority order. |

---

### Want to support the project?

_If this software is useful and brings you value,
consider supporting the project by buying me a coffee.
Your support helps keep development going._

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/shukurenais)

## API Credits

[Song.link](https://song.link) · [hifi-api](https://github.com/binimum/hifi-api) · [dabmusic.xyz](https://dabmusic.xyz) · [SpotubeDL](spotubedl.com) · [afkarxyz](https://github.com/afkarxyz) · [MusicBrainz](https://musicbrainz.org)

> [!TIP]
>
> **Star Us**, You will receive all release notifications from GitHub without any delay ~
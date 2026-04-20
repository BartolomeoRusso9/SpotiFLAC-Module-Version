
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
New use:
```spotiflac 
> spotiflac url ./out --service tidal spoti --use-artist-subfolders
```
---

## Advanced Configuration

You can customize the download behavior, prioritize specific streaming services, and organize your files automatically into folders.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ",
    output_dir="./MusicLibrary",
    services=["qobuz", "amazon", "tidal", "spoti", "youtube"],
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

### Setting the Token

#### Environment Variable (all platforms)

The recommended approach across all systems:

```bash
export QOBUZ_AUTH_TOKEN="your_token_here"
```
On Windows (Command Prompt):
```bash
set QOBUZ_AUTH_TOKEN=your_token_here
```
On Windows (PowerShell):
```bash
$env:QOBUZ_AUTH_TOKEN="your_token_here"
```
To make it permanent on Linux/macOS, add the export line to your **~/.bashrc, ~/.zshrc**, or equivalent shell config file.

**Python**
```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
    output_dir="./downloads",
    qobuz_token="your_token_here"
)
```
Alternatively, set the environment variable before running and omit the parameter entirely.
**Docker**
Pass the token as an environment variable at runtime:
```bash
docker run \
  -e QOBUZ_AUTH_TOKEN="your_token_here" \
  -v ./downloads:/downloads \
  your-spotiflac-image
```

Or define it in your **docker-compose.yml**:
```yaml
services:
    spotiflac:
        image: your-spotiflac-image
        environment:
            - QOBUZ_AUTH_TOKEN=your_token_here
        volumes:
            - ./downloads:/downloads
```

> **Never hardcode the token in a Dockerfile**: use environment variables or a .env file (excluded from version control via .gitignore).

**.env File**
If you prefer a local config file (useful for development):
```env
QOBUZ_AUTH_TOKEN=your_token_here
```
Load it before running:
```bash
export $(cat .env | xargs) && python launcher.py ...
```
Or with Docker Compose:
```yaml
services:
  spotiflac:
    env_file:
      - .env
```
> Add **.env** to your **.gitignore** to avoid accidentally committing your token.


<h2>CLI program usage</h2>
<p>Program can be downloaded for <b>Windows</b>, <b>Linux (x86 and ARM)</b> and <b>MacOS</b>. The downloads are available under the releases.<br>
Program can also be ran by downloading the python files and calling <code>python launcher.py</code> with the arguments.</p>

<h4>Windows example usage:</h4>

```bash
./SpotiFLAC-Windows.exe url
                        output_dir
                        [--service tidal qobuz spoti youtube amazon]
                        [--filename-format "{title} - {artist}"]
                        [--use-track-numbers] [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--loop minutes]
                        
```

<h4>Linux / Mac example usage:</h4>

```bash
chmod +x SpotiFLAC-Linux-arm64
./SpotiFLAC-Linux-arm64 url
                        output_dir
                        [--service tidal qobuz spoti youtube amazon]
                        [--filename-format "{title} - {artist}"]
                        [--use-track-numbers] [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--loop minutes]
                        
```
---

## API Reference

### `SpotiFLAC()` Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| **`url`** | `str` | *Required* | The Spotify URL (Track, Album, or Playlist) you want to download. |
| **`output_dir`** | `str` | *Required* | The destination directory path where the audio files will be saved. |
| **`services`** | `list` | `["tidal", "deezer", "qobuz", "spoti", "youtube", "amazon"]` | Specifies which services to use and their priority order. |
| **`filename_format`** | `str` | `"{title} - {artist}"` | Format for naming downloaded files. See placeholders below. |
| **`use_track_numbers`** | `bool` | `False` | Prefixes the filename with the track number. |
| **`use_artist_subfolders`** | `bool` | `False` | Automatically organizes downloaded files into subfolders by artist. |
| **`use_album_subfolders`** | `bool` | `False` | Automatically organizes downloaded files into subfolders by album. |
| **`loop`** | `int` | `None` | Duration in minutes to keep retrying failed downloads. |
| **`qobuz_token`** | `str` | `None` | Optional Qobuz user auth token used as fallback for metadata resolution. |


### Filename Format Placeholders

When customizing the `filename_format` string, you can use the following dynamic tags:

* `{title}` - Track title
* `{artist}` - Track artist
* `{album}` - Album name
* `{track}` - Track number
* `{date}` - Full release date (e.g., YYYY-MM-DD)
* `{year}` - Release year (e.g., YYYY)
* `{position}` - Playlist position
* `{isrc}` - Track ISRC code
* `{duration}` - Track duration (MM:SS)
### Want to support the project?

_If this software is useful and brings you value,
consider supporting the project by buying me a coffee.
Your support helps keep development going._

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/shukurenais)

## API Credits

[Song.link](https://song.link) · [hifi-api](https://github.com/binimum/hifi-api) · [dabmusic.xyz](https://dabmusic.xyz) · [spotidownloader](https://spotidownloader.com) · [SpotubeDL](spotubedl.com) · [afkarxyz](https://github.com/afkarxyz)

> [!TIP]
>
> **Star Us**, You will receive all release notifications from GitHub without any delay ~

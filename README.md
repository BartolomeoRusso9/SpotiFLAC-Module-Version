
# SpotiFLAC Python Module

[![PyPI version](https://img.shields.io/pypi/v/SpotiFLAC.svg)](https://pypi.org/project/SpotiFLAC)
[![Python Versions](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)


Integrate **SpotiFLAC** directly into your Python projects. Perfect for building custom Telegram bots, automation tools, bulk downloaders, or web interfaces.

> **Looking for a standalone app?** > Check out the [CLI tool](https://github.com/jelte1/SpotiFLAC-Command-Line-Interface) or the [GUI tool](https://github.com/afkarxyz/SpotiFLAC).

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
> spotiflac --service qobuz tidal --use-artist-subfolders url ./out 
```
---

## Advanced Configuration

You can customize the download behavior, prioritize specific streaming services, and organize your files automatically into folders.

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ",
    output_dir="./MusicLibrary",
    services=["qobuz", "amazon", "tidal", "deezer"],
    filename_format="{year} - {album}/{track}. {title}",
    use_artist_subfolders=True,
    use_album_subfolders=True,
    loop=60 # Retry duration in minutes
)

```
<h2>CLI program usage</h2>
<p>Program can be downloaded for <b>Windows</b>, <b>Linux (x86 and ARM)</b> and <b>MacOS</b>. The downloads are available under the releases.<br>
Program can also be ran by downloading the python files and calling <code>python launcher.py</code> with the arguments.</p>

<h4>Windows example usage:</h4>

```bash
./SpotiFLAC-Windows.exe [--service tidal qobuz deezer amazon]
                        [--filename-format "{title} - {artist}"]
                        [--use-track-numbers] [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--loop minutes]
                        url
                        output_dir
```

<h4>Linux / Mac example usage:</h4>

```bash
chmod +x SpotiFLAC-Linux-arm64
./SpotiFLAC-Linux-arm64 [--filename-format "{title} - {artist}"]
                        [--use-track-numbers] [--use-artist-subfolders]
                        [--use-album-subfolders]
                        [--loop minutes]
                        url
                        output_dir
```
---

## API Reference

### `SpotiFLAC()` Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| **`url`** | `str` | *Required* | The Spotify URL (Track, Album, or Playlist) you want to download. |
| **`output_dir`** | `str` | *Required* | The destination directory path where the audio files will be saved. |
| **`services`** | `list` | `["tidal", "deezer", "qobuz", "amazon"]` | Specifies which services to use and their priority order. |
| **`filename_format`** | `str` | `"{title} - {artist}"` | Format for naming downloaded files. See placeholders below. |
| **`use_track_numbers`** | `bool` | `False` | Prefixes the filename with the track number. |
| **`use_artist_subfolders`** | `bool` | `False` | Automatically organizes downloaded files into subfolders by artist. |
| **`use_album_subfolders`** | `bool` | `False` | Automatically organizes downloaded files into subfolders by album. |
| **`loop`** | `int` | `None` | Duration in minutes to keep retrying failed downloads. |

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

## API Credits

[Song.link](https://song.link) · [hifi-api](https://github.com/binimum/hifi-api) · [dabmusic.xyz](https://dabmusic.xyz) · [yoinkify.lol](https://github.com/chasemarshall/yoink)

> [!TIP]
>
> **Star Us**, You will receive all release notifications from GitHub without any delay ~

<!-- Extracted verbatim from README.md. The README had grown to 76 KB
     and 87 headings, which is past the point where either GitHub or
     PyPI renders it usefully. Nothing here was reworded in the split. -->

[← Back to the README](../../README.md)

# Advanced Configuration

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
    # Lyrics providers, not audio ones: run_health_check probes the
    # servers listed in core/health_check.py (apple, lrclib,
    # musixmatch, spotify, deezer, genius, netease, qq, youtube,
    # kugou). An "ext:..." id matches nothing there.
    results = await run_health_check(["apple", "lrclib", "musixmatch"])
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

### Lyrics Provider Order

`lyrics_providers` (`--lyrics-providers`) is a **ranking**, not a set. Every provider on the list is queried at once — that is what keeps the lookup fast — but the answers are read back in the order you gave, and the first one that has lyrics wins.

The order matters because the providers do not return the same thing. Apple returns *word-by-word* lyrics, timed per syllable:

```
[00:08.75]<00:08.75>Sento <00:09.05>un<00:09.22>ra-<00:09.41>ta- <00:09.90>ta
```

LRCLIB returns line-level lyrics. Put `apple` first and you get the first form wherever Apple has it, falling through to the rest where it does not — at no cost in speed, because by the time a first choice comes back empty the others have long since finished.

Each provider's own answer is cached separately (7 days for a hit, 6 hours for a miss), so changing the order takes effect immediately rather than being masked by a cached result from a previous ordering.

### Apple Lyrics: Word-by-Word or Line-Synced

By default Apple's lyrics are kept in their native *word-by-word* form (timed per syllable, with inline `<mm:ss.xx>` tags). Set `apple_lyrics_word_by_word=False` (Python / GUI toggle) or pass `--apple-lyrics-line-synced` (CLI) to drop the per-syllable timings and keep only line-level timing:

```
# word-by-word (default)
[00:08.75]<00:08.75>Sento <00:09.05>un<00:09.22>ra-<00:09.41>ta- <00:09.90>ta

# line-synced (--apple-lyrics-line-synced)
[00:08.75]Sento unra-ta- ta
```

Line-synced is useful for players and overlay apps that only understand line-level `.lrc`, or simply if you prefer a plain karaoke-style scroll. The setting affects only the `apple` provider; every other provider is unchanged. Apple's two renderings are cached under separate keys, so switching the setting takes effect immediately.

### Lyrics Files (`.lrc`)

Lyrics are written into the audio file's tag by default. That is enough for tagging, and not enough for playback: **no major player renders word-by-word lyrics out of an embedded tag.** Apple Music in particular strips the inline timing from a local file and shows flat text, and the synced lyrics it scrolls for streamed tracks come from Apple's own servers, not from the file.

The synced display comes from an `.lrc` file on disk, read by a player or an overlay app. Two switches, because two kinds of player disagree about where to look:

| Option | Writes |
| --- | --- |
| `save_lrc` / `--save-lrc` | `<audio file's name>.lrc`, next to the track |
| `lrc_library_dir` / `--lrc-dir DIR` | `DIR/Artist - Title.lrc`, one folder for everything |

The first is the convention players pair a sidecar with its track by; the second is the `Artist - Title` layout overlay apps (LyricsX and similar) look lyrics up by — note it is the *reverse* of the default `{title} - {artist}` filename format, which is why it cannot simply reuse the audio file's name.

```bash
# Both layouts at once
spotiflac https://open.spotify.com/playlist/... ./out \
    --lyrics-providers apple lrclib \
    --save-lrc --lrc-dir ~/Music/Lyrics
```

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/playlist/...",
    output_dir="./out",
    lyrics_providers=["apple", "lrclib"],
    save_lrc=True,
    lrc_library_dir="~/Music/Lyrics",
)
```

Both are off by default. The lyrics are read back out of the finished file rather than fetched again, so there is no extra network request, it works with every provider, and the `.lrc` is by construction identical to what the track carries — including after transcoding, where the sidecar follows the converted file. A track with no lyrics produces no empty file, and a destination that cannot be written is logged as a warning rather than failing the download.

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

### Transcoding

Downloads use the selected quality profile: `HI_RES_LOSSLESS` requests the best available lossless tier, while `LOSSLESS` requests standard lossless audio. Set `transcode_to` (Python) or `--transcode` (CLI) to convert every finished track to a single format of your choice. Tags, cover art and lyrics are carried over to the new file, and the original is deleted once the conversion succeeds unless `transcode_keep_original` / `--keep-original` is set.

| Value | Extension | Kind | Why you'd pick it |
| --- | --- | --- | --- |
| `flac` | `.flac` | lossless | The universal default — read by everything except Finder previews on macOS |
| `alac` | `.m4a` | lossless | Apple Lossless. The one lossless format macOS reads natively: Finder shows the cover art, Music.app and QuickLook just work |
| `wavpack` | `.wv` | lossless | Compresses a little better than FLAC; narrower player support |
| `tta` | `.tta` | lossless | True Audio — very cheap to decode, niche support |
| `wav` | `.wav` | lossless, uncompressed | For DJ software and audio editors that want raw PCM. Roughly 1.6× the size of FLAC |
| `aiff` | `.aiff` | lossless, uncompressed | The Apple flavour of raw PCM, same size as WAV |
| `mp3` | `.mp3` | lossy | 320 kbps by default, for players or car stereos that cannot handle lossless files |

**The lossless targets are bit-exact.** The sample rate and bit depth of the source are carried over untouched — no resampling, no requantisation — so a 24-bit/96 kHz FLAC converted to ALAC decodes back to byte-identical PCM. Only `mp3` re-encodes the audio, and only it reads `transcode_bitrate` / `--transcode-bitrate`; the lossless encoders ignore it.

Converting a *lossy* source to a lossless target is allowed but logs a warning: the output is a faithful copy of an already-degraded signal, not a recovered original, and it will be several times larger.

Requires `ffmpeg`. Checked upfront — before any track downloads — so you never download a whole album only to fail at the conversion step. If it's not on your `PATH`, SpotiFLAC automatically attempts to install it right there, printing progress as it goes (`core/ffmpeg_check.py`), using the same package managers and privilege rules as the Node.js auto-install described above (never escalates privileges itself); the run only fails if that attempt doesn't work out. Tidal FLAC muxing and Amazon decryption also need ffmpeg but have no such auto-install — they just fail if it's missing, same as before.

```bash
# CLI — every track ends up as a 320 kbps MP3
spotiflac https://open.spotify.com/album/... ./out --service ext:tidal-web --mp3

# Keep the FLAC too, and use 192 kbps instead
spotiflac https://open.spotify.com/album/... ./out --mp3 --transcode-bitrate 192k --keep-original

# Lossless, in a container macOS reads natively (--alac is shorthand)
spotiflac https://open.spotify.com/album/... ./out --alac

# Any other lossless target
spotiflac https://open.spotify.com/album/... ./out --transcode wavpack
```

```python
# Python API
from SpotiFLAC import SpotiFLAC
SpotiFLAC(
    url="https://open.spotify.com/album/...",
    output_dir="./downloads",
    services=["ext:tidal-web", "ext:qobuz-web"],
    transcode_to="alac",          # or flac / wav / aiff / wavpack / tta / mp3
    transcode_bitrate="320k",     # mp3 only; ignored by the lossless targets
)
```

**Skipping already-downloaded tracks still works.** The converted file keeps the exact name the extension would have used, only with the target extension, so SpotiFLAC looks for that file *before* contacting any extension and skips the track when it is already there — no network request, no re-encode. Running the same album twice therefore costs nothing the second time. A leftover file from an earlier run in another format is converted in place instead of being re-downloaded, so an existing library converges to the chosen format in a single pass.

The conversion is a no-op for extensions that already deliver the requested format, which are passed through untouched. For `alac` the *codec* is checked rather than the extension, since a lossy AAC download also lands in an `.m4a` and must not be mislabelled as lossless.

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
- **Skipped automatically for lossy output.** If `transcode_to="mp3"` (or `--mp3`) is set, the already-lossy result is never analyzed — checking an MP3 for ultrasonic content would be meaningless. The lossless targets keep the check, since they preserve the spectrum of the source exactly.
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

### Watch Mode (keep syncing on an interval)

Add `--watch MINUTES` to any run — a single URL, or one or more `--playlist` — to re-run the exact same sync every N minutes, forever, instead of exiting after one pass:

```bash
# Re-check this playlist every hour for new tracks
spotiflac https://open.spotify.com/playlist/... ./Music --service ext:tidal-web --watch 60
```

Every download path already indexes what's on disk and skips it (by ISRC/tags for `--playlist`, by filename otherwise — see [Multiple Playlists in One Folder](#multiple-playlists-in-one-folder) above and the download flow in general), so each cycle after the first is cheap: it only fetches tracks that are actually new. Stop it with Ctrl+C.

`--watch` is a different tool from `--loop`: `--loop` retries *failed* tracks for a bounded time after one session ends; `--watch` re-runs the *whole* sync indefinitely. Combine both if you want each cycle to also retry transient failures:

```bash
spotiflac https://open.spotify.com/album/... ./Music --watch 1440 --loop 30
```

`--watch` is saved/restored by `--save-profile`/`--profile` like any other flag. Not available in the terminal UI (`--tui`), and it does not cover Spotify's "Liked Songs" — that's a private, per-account list that would need a full Spotify login (OAuth) to read, which this project deliberately doesn't implement (see the "no-account" design goal throughout this README). Point `--watch` at a public playlist, album, or artist URL instead.

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

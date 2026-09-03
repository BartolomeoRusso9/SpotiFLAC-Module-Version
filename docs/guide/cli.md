<!-- Extracted verbatim from README.md. The README had grown to 76 KB
     and 87 headings, which is past the point where either GitHub or
     PyPI renders it usefully. Nothing here was reworded in the split. -->

[← Back to the README](../../README.md)

# CLI Usage

## CLI Usage (standalone executables)

The blocks below are usage synopses, not commands to copy: the `[...]`
brackets mark optional arguments, and each option is on its own line for
readability. Marked `text` rather than `bash` so nothing suggests otherwise.

```text
./SpotiFLAC-Windows.exe url
                        output_dir
                        [--csv tracks.csv]
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
                        [--apple-lyrics-line-synced]
                        [--no-enrich]
                        [--enrich-providers deezer apple qobuz tidal]
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
                        [--csv tracks.csv]
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
                        [--apple-lyrics-line-synced]
                        [--no-enrich]
                        [--enrich-providers deezer apple qobuz tidal]
                        [--retries N]
                        [--post-action none|open_folder|notify|command]
                        [--post-command "CMD with {folder} {succeeded} {skipped} {failed}"]
                        [--profile NAME]
                        [--save-profile NAME]
```

*(For ARM devices like Raspberry Pi, replace `x86_64` with `arm64`)*

> **Reminder:** `--service` values only resolve to something functional if you have already installed a matching extension (`--service ext:tidal-web` needs the `tidal-web` extension installed from a registry you configured). See [Extensions](extensions.md#extensions).

---

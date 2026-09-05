"""cli_preview.py — The command line a configuration is equivalent to.

Born inside the guided mode, which printed it as a parting gift: the guided
mode is fifteen questions, and the CLI does the same run in one line that can
be scripted, scheduled, or pasted into an issue. Showing the equivalent
command is how somebody graduates from the one to the other.

It lives here rather than in a frontend because the TUI shows the same thing
as a panel that updates while the options change, and because it is the
readable form of the same `cfg` dict the launcher spreads over
`_run_download_async` — when a new flag is added, this is the second place
that has to learn about it, and a shared module makes that one place instead
of one per UI.

`build_command_parts()` returns the argv the run corresponds to;
`format_command()` renders it as the multi-line, shell-quoted string a person
would paste. Neither prints anything: the caller owns the screen.
"""

from __future__ import annotations

import logging
import shlex

from .transcode import is_lossless


def is_playlist_url(url: str) -> bool:
    """Whether a link stands for a set of tracks rather than a single one.

    Decides whether the playlist-subfolder flag is worth printing at all,
    and — in a guided frontend — whether the question behind it is worth
    asking: for a single track both would be noise.
    """
    lower_url = (url or "").lower()
    return "/playlist/" in lower_url or "list=" in lower_url or "/sets/" in lower_url


def build_command_parts(cfg: dict) -> list[str]:
    """The argv equivalent of *cfg*, as an unquoted list.

    Only settings that differ from the CLI's own defaults are emitted — a
    command spelling out every default would be unreadable, and every flag
    printed here is one the user actually chose.
    """
    parts = (
        ["spotiflac", "--csv", cfg["csv_path"], cfg["output_dir"]]
        if cfg.get("csv_path")
        else ["spotiflac", cfg["url"], cfg["output_dir"]]
    )
    if cfg.get("output_path"):
        parts.extend(["-o", cfg["output_path"]])
    parts.extend(["-s", *cfg["services"]])
    if cfg["quality"] not in ("LOSSLESS", "BEST"):
        parts.extend(["-q", cfg["quality"]])
    if cfg["filename_format"] != "{title} - {artist}":
        parts.extend(["--filename-format", cfg["filename_format"]])
    if cfg["use_track_numbers"]:
        parts.append("--use-track-numbers")
    if cfg["use_album_track_numbers"]:
        parts.append("--use-album-track-numbers")
    if cfg["use_artist_subfolders"]:
        parts.append("--use-artist-subfolders")
    if cfg["use_album_subfolders"]:
        parts.append("--use-album-subfolders")

    # Check if URL is a playlist before appending the CLI flag
    is_playlist = is_playlist_url(cfg["url"])
    if is_playlist:
        parts.append(
            "--playlist-subfolders"
            if cfg.get("create_playlist_subfolders", True)
            else "--no-playlist-subfolders"
        )

    if cfg["first_artist_only"]:
        parts.append("--first-artist-only")
    if cfg.get("artist_separator"):
        parts.extend(["--artist-separator", cfg["artist_separator"]])
    if not cfg["embed_lyrics"]:
        parts.append("--no-lyrics")
    else:
        parts.extend(["--lyrics-providers", *cfg["lyrics_providers"]])
        if not cfg.get("apple_lyrics_word_by_word", True):
            parts.append("--apple-lyrics-line-synced")
    if not cfg["enrich_metadata"]:
        parts.append("--no-enrich")
    else:
        parts.extend(["--enrich-providers", *cfg["enrich_providers"]])
    if cfg.get("transcode_to"):
        # --transcode rather than the --mp3/--alac shorthands: those cover
        # two of the seven targets, and one spelling keeps the printed
        # command readable however the wizard was answered.
        parts.extend(["--transcode", cfg["transcode_to"]])
        # Only the lossy targets read a bitrate. Printing --transcode-bitrate
        # beside --transcode flac would advertise a knob that does nothing.
        if not is_lossless(cfg["transcode_to"]) and (
            cfg.get("transcode_bitrate", "320k") != "320k"
        ):
            parts.extend(["--transcode-bitrate", cfg["transcode_bitrate"]])
        if cfg.get("transcode_keep_original"):
            parts.append("--keep-original")
    if cfg.get("track_max_retries"):
        parts.extend(["--retries", str(cfg["track_max_retries"])])
    if cfg.get("max_concurrent_downloads", 2) != 2:
        parts.extend(["--max-concurrent", str(cfg["max_concurrent_downloads"])])
    if cfg.get("timeout_s"):
        parts.extend(["--timeout", str(cfg["timeout_s"])])
    if cfg.get("post_download_action") and cfg["post_download_action"] != "none":
        parts.extend(["--post-action", cfg["post_download_action"]])
        if cfg["post_download_action"] == "command" and cfg.get(
            "post_download_command",
        ):
            parts.extend(["--post-command", cfg["post_download_command"]])
    if cfg.get("save_lrc"):
        parts.append("--save-lrc")
    if cfg.get("lrc_library_dir"):
        parts.extend(["--lrc-dir", cfg["lrc_library_dir"]])
    if cfg.get("log_level") is not None:
        # Profiles store the numeric constant; print the name a user would
        # actually type, which --log-level accepts either way.
        level = cfg["log_level"]
        name = logging.getLevelName(level) if isinstance(level, int) else str(level)
        parts.extend(["--log-level", name])
    # Enabled by default, so only the opt-out is worth printing.
    if cfg.get("allow_fallback", True) is False:
        parts.append("--no-fallback")
    if cfg.get("include_featuring"):
        parts.append("--include-featuring")
    if cfg.get("m3u_format", "m3u8") != "m3u8":
        parts.extend(["--m3u", cfg["m3u_format"]])
    if cfg.get("verify_hires"):
        parts.append("--verify-hires")
    # Resuming is the default, so only its absence is worth spelling out.
    if cfg.get("resume", True) is False:
        parts.append("--no-resume")
    for hook in cfg.get("post_download_hooks") or []:
        parts.extend(["--post-hook", hook])
    if cfg.get("qobuz_local_api_url"):
        parts.extend(["--qobuz-local-api", cfg["qobuz_local_api_url"]])
    if cfg.get("tidal_custom_api"):
        parts.extend(["--tidal-api", cfg["tidal_custom_api"]])
    if cfg.get("loop"):
        parts.extend(["--loop", str(cfg["loop"])])
    if cfg.get("watch"):
        parts.extend(["--watch", str(cfg["watch"])])

    return parts


def format_command(cfg: dict) -> str:
    """`build_command_parts()` as a shell-quoted, line-broken string.

    One line per flag, with its values beside it, rather than one line per
    token. The wizard printed this once at the end of a session and a
    twenty-line command was merely ugly; the TUI shows it as a panel that
    redraws while the options change, where it also has to be *readable* —
    `--lyrics-providers apple lrclib` says one thing, and splitting it across
    three lines says nothing.
    """
    lines: list[str] = []
    for part in build_command_parts(cfg):
        quoted = shlex.quote(part)
        # A new line per flag; everything after one belongs to it.
        if part.startswith("-") or not lines:
            lines.append(quoted)
        else:
            lines[-1] += f" {quoted}"
    return " \\\n    ".join(lines)

#!/usr/bin/env python3
"""CLI entry point for SpotiFLAC.

=== Async migration ===
The entire entry point now runs on a single shared event loop instead of
delegating to `SpotiFLAC(...)` (a sync wrapper that opens its own
asyncio.run() internally). It uses `SpotiflacDownloader` directly, which is
already 100% async-native, and that is why `check_for_updates_async`
and `run_interactive()` can now be awaited in the same loop instead of
opening a new one each time.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable

from .check_update import check_for_updates_async
from .client import _CleanConsoleFormatter
from .downloader import DownloadOptions, SpotiflacDownloader
from .core.library_notify import LIBRARY_TOKEN_ENV, LIBRARY_USER_ENV
from .core.library_notify import SUPPORTED as LIBRARY_SUPPORTED
from .core.report import RunReport
from .interactive import run_interactive


from .extensions.trust import TRUST_TIERS


def _early_urls_from_argv(flag: str) -> list[str]:
    """Best-effort scan of raw sys.argv for repeated `flag URL` occurrences.

    Runs before the full `argparse` parse (and before the early
    `ExtensionManager` bootstrap in `amain()`) so that any registry/directory
    passed on this invocation is persisted in time to affect the same run's
    automatic extension install, not just future ones. Mirrors the ad-hoc
    `--host`/`--port` mini-scan already used for `--web` below.

    Handles both space-separated (`flag URL`) and equals-sign (`flag=URL`)
    forms, mirroring argparse behavior. Shared by `--registries` and
    `--registry-directories`, which persist the same way.
    """
    urls: list[str] = []
    argv = sys.argv[1:]
    for i, token in enumerate(argv):
        if token == flag and i + 1 < len(argv):
            urls.append(argv[i + 1])
        elif token.startswith(f"{flag}="):
            urls.append(token.split("=", 1)[1])
    return urls


def _early_registries_from_argv() -> list[str]:
    return _early_urls_from_argv("--registries")


def _early_registry_directories_from_argv() -> list[str]:
    return _early_urls_from_argv("--registry-directories")


def _early_min_trust_from_argv() -> str | None:
    """Reads --min-trust-tier before argparse runs.

    The extension bootstrap happens near the top of amain(), well before the
    full parser is built — and the whole point of a trust floor is that it
    applies to that bootstrap, which is the code path that installs and then
    executes third-party code. Reading it late would enforce it everywhere
    except where it matters most.
    """
    # Last occurrence wins, because that is what argparse does with a
    # repeated option: stopping at the first one would bootstrap under
    # `--min-trust-tier unverified --min-trust-tier signed` with no floor at
    # all, while the parse further down reported "signed" — the bootstrap
    # being exactly the step that installs and runs the extension. An
    # unknown tier needs no check here: ExtensionManager rejects it (see
    # extensions/trust.py normalise_min_trust), the bootstrap is skipped,
    # and argparse's own `choices` reports it.
    argv = sys.argv[1:]
    found: str | None = None
    for i, token in enumerate(argv):
        if token == "--min-trust-tier" and i + 1 < len(argv):
            found = argv[i + 1]
        elif token.startswith("--min-trust-tier="):
            found = token.split("=", 1)[1]
    return found


def _register_cli_registries(urls: list[str]) -> None:
    """Persists `--registries` URLs via `extensions.registry_config`, so they
    are merged into `registry_config.effective_urls()` the same way
    `SPOTIFLAC_REGISTRIES`, `.env`-file, or GUI/Interactive-added ones are.
    """
    if not urls:
        return
    from .extensions import registry_config

    for url in urls:
        try:
            registry_config.add_registry(url)
        except Exception as e:
            print(f"Unable to add registry '{url}': {e}", file=sys.stderr)


def _register_cli_registry_directories(urls: list[str]) -> None:
    """Persists `--registry-directories` URLs the same way
    `_register_cli_registries` does for `--registries` — see
    extensions/directories.py.
    """
    if not urls:
        return
    from .extensions import directories

    for url in urls:
        try:
            directories.add_directory(url)
        except Exception as e:
            print(f"Unable to add registry directory '{url}': {e}", file=sys.stderr)


def _print_welcome_banner() -> None:
    """Prints a one-time ASCII banner with project/community links on startup.

    Suppressed entirely under --json: it goes to stdout, which in that mode
    carries the report document and nothing else. `spotiflac ... --json | jq`
    would otherwise be fed an ASCII logo before the JSON. Console output from
    core/console._write already goes to stderr; this was the one thing that
    didn't.

    Shown for every launch mode (CLI, --interactive, --gui, --web) since it
    runs as the very first thing in amain(), before any mode-specific setup.
    Colors and links are skipped for non-tty output (piped/redirected) or when
    NO_COLOR is set, matching the convention used elsewhere (interactive.py).
    """
    if "--json" in sys.argv:
        return

    no_color = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

    def c(code: str, text: str) -> str:
        return text if no_color else f"\033[{code}m{text}\033[0m"

    def format_link(url: str) -> str:
        # Makes the URL clickable via OSC 8 and styles it underlined cyan (4;36)
        styled_url = url if no_color else f"\033[4;36m{url}\033[0m"
        return url if no_color else f"\033]8;;{url}\033\\{styled_url}\033]8;;\033\\"

    try:
        version = importlib.metadata.version("spotiflac")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    # Logo esteso "SpotiFLAC Python Module" in font Slant, verde brillante (1;92)
    ascii_logo = [
        c(
            "1;92",
            r"   _____             __  _ ________    ___  ______   ____        __  __                  __  ___          __      __     ",
        ),
        c(
            "1;92",
            r"  / ___/____  ____  / /_(_) ____/ /   /   |/ ____/  / __ \__  __/ / / /_  ____  ____    /  |/  /___  ____/ /_  __/ /___  ",
        ),
        c(
            "1;92",
            r"  \__ \/ __ \/ __ \/ __/ / /_  / /   / /| / /      / /_/ / / / / /_/ __ \/ __ \/ __ \  / /|_/ / __ \/ __  / / / / / __ \ ",
        ),
        c(
            "1;92",
            r" ___/ / /_/ / /_/ / /_/ / __/ / /___/ ___ / /___  / ____/ /_/ / __/ / / / /_/ / / / / / /  / / /_/ / /_/ / /_/ / /  __/  ",
        ),
        c(
            "1;92",
            r"/____/ .___/\____/\__/_/_/   /_____/_/  |_\____/ /_/    \__, /_/ /_/ /_/\____/_/ /_/ /_/  /_/\____/\__,_/\__,_/_/\___/   ",
        ),
        c(
            "1;92",
            r"    /_/                                                 /____/                                                           ",
        ),
    ]

    print()
    for line in ascii_logo:
        print(line)

    # Crea un "Badge" con sfondo ciano (46), testo nero (30) e grassetto (1)
    version_badge = c("1;30;46", f" v{version} ")

    print()
    print(f"  {c('1;90', '▪')} {c('1;37', 'Version')}   {version_badge}")
    print(
        f"  {c('1;90', '▪')} {c('1;37', 'Author')}    {c('1;93', 'BartolomeoRusso9')}"
    )
    print(
        f"  {c('1;90', '▪')} {c('1;37', 'GitHub')}    {format_link('https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version')}"
    )
    print(
        f"  {c('1;90', '▪')} {c('1;37', 'Telegram')}  {format_link('https://t.me/SpotiFLAC_Chat')}"
    )
    print(
        f"  {c('1;90', '▪')} {c('1;37', 'Support')}   {format_link('https://ko-fi.com/bartolomeorusso9')}"
    )
    print()


def load_config() -> dict:
    config_path = "config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                raw = json.load(f)
            from .core.profiles import ProfileConfig

            return ProfileConfig.model_validate(raw).model_dump(exclude_none=True)
        except json.JSONDecodeError:
            pass
        except Exception:
            pass
    return {}


async def _load_profile_into_defaults(profile_name: str) -> dict:
    """Return profile data dict, or empty dict on failure."""
    try:
        from .core.profiles import get_profile_async

        data = await get_profile_async(profile_name)
        if data:
            return data
    except Exception:
        pass
    return {}


def _resolve_log_level(verbose: bool) -> int:
    """Hide warnings unless the user explicitly asked for verbose logging."""
    return logging.DEBUG if verbose else logging.ERROR


def parse_args(profile_defaults: dict | None = None) -> argparse.Namespace:
    pd = profile_defaults or {}

    parser = argparse.ArgumentParser(
        prog="spotiflac",
        description="Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube, Pandora and more.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "url",
        nargs="?",
        help="Spotify, Tidal, Apple Music, SoundCloud, YouTube or Pandora URL",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=pd.get("output_dir"),
        help="Destination directory",
    )

    # ── Multi-playlist ──────────────────────────────────────────────────────
    playlists_grp = parser.add_argument_group("Multi-Playlist")
    playlists_grp.add_argument(
        "--playlist",
        "-p",
        action="append",
        dest="playlists",
        metavar="URL",
        help="Playlist to sync; repeat the flag for each one "
        "(spotiflac -p URL1 -p URL2 DEST). Every track lands in a single "
        "destination folder: tracks shared by several playlists are downloaded "
        "once, tracks already in the folder are never downloaded again, and "
        "each playlist gets an M3U file listing its own tracks in order.",
    )
    playlists_grp.add_argument(
        "--m3u",
        choices=["m3u8", "m3u", "none"],
        default=pd.get("m3u_format", "m3u8"),
        dest="m3u_format",
        help="Playlist file written for each --playlist, in the destination "
        "folder with paths relative to it (default: m3u8). It is rewritten "
        "only when its content changed. 'none' disables it.",
    )

    def _service_type(value: str) -> str:
        from .extensions.catalog import known_service

        if known_service(value):
            return value
        msg = f"invalid service: '{value}'. Use ext:<name> or a supported compatibility alias."
        raise argparse.ArgumentTypeError(
            msg,
        )

    parser.add_argument(
        "--service",
        "-s",
        type=_service_type,
        nargs="+",
        default=pd.get("services", ["ext:tidal-web"]),
        metavar="SERVICE",
        help="Extension providers in priority order (default: ext:tidal-web). "
        "Use ext:<name>; historical service aliases remain supported.",
    )
    parser.add_argument(
        "--filename-format",
        "-f",
        default=pd.get("filename_format", "{title} - {artist}"),
        dest="filename_format",
        help="Filename template with placeholders",
    )
    parser.add_argument(
        "--output-path",
        "-o",
        default=pd.get("output_path", None),
        dest="output_path",
        metavar="FILE",
        help="Exact output file path for single track downloads",
    )
    parser.add_argument(
        "--quality",
        "-q",
        default=pd.get("quality", "LOSSLESS"),
        help="Quality: HI_RES_LOSSLESS (best available) or LOSSLESS. "
        "DOLBY_ATMOS is also accepted but is Tidal-exclusive — any other "
        "provider falls back to HI_RES_LOSSLESS instead of using it. "
        "Legacy provider-specific values remain accepted. Default: LOSSLESS",
    )
    parser.add_argument(
        "--use-track-numbers",
        action="store_true",
        dest="use_track_numbers",
        default=pd.get("use_track_numbers", False),
    )
    parser.add_argument(
        "--use-album-track-numbers",
        action="store_true",
        dest="use_album_track_numbers",
        default=pd.get("use_album_track_numbers", False),
    )
    parser.add_argument(
        "--use-artist-subfolders",
        action="store_true",
        dest="use_artist_subfolders",
        default=pd.get("use_artist_subfolders", False),
    )
    parser.add_argument(
        "--use-album-subfolders",
        action="store_true",
        dest="use_album_subfolders",
        default=pd.get("use_album_subfolders", False),
    )
    parser.add_argument(
        "--playlist-subfolders",
        action="store_true",
        dest="create_playlist_subfolders",
        default=pd.get("create_playlist_subfolders", True),
        help="Create a subfolder for playlist downloads (default: enabled).",
    )
    parser.add_argument(
        "--no-playlist-subfolders",
        action="store_false",
        dest="create_playlist_subfolders",
        help="Keep playlist downloads in the output directory.",
    )
    parser.add_argument(
        "--first-artist-only",
        action="store_true",
        dest="first_artist_only",
        default=pd.get("first_artist_only", False),
    )
    parser.add_argument(
        "--artist-separator",
        dest="artist_separator",
        default=pd.get("artist_separator", None),
        metavar="SEP",
        help="Join multiple artists into one ARTIST/ALBUMARTIST tag with this "
        "separator (e.g. ', ' or ' / ') instead of writing them as a "
        "multi-value field. Useful for players like Rekordbox that join "
        "multi-value fields with a bare space, mashing artist names "
        "together with no separator at all.",
    )
    parser.add_argument(
        "--include-featuring",
        action="store_true",
        dest="include_featuring",
        default=pd.get("include_featuring", False),
        help="Include featured artist tracks when downloading artist discographies.",
    )
    parser.add_argument(
        "--qobuz-local-api",
        default=pd.get("qobuz_local_api_url", None),
        dest="qobuz_local_api_url",
        metavar="URL",
    )
    parser.add_argument(
        "--tidal-api",
        default=pd.get("tidal_custom_api", None),
        dest="tidal_custom_api",
        metavar="URL",
        help="URL of a self-hosted hifi-api instance (https://github.com/binimum/hifi-api). "
        "Takes priority over built-in API pool.",
    )
    parser.add_argument(
        "--registries",
        action="append",
        default=None,
        dest="registries",
        metavar="URL",
        help="An extension-registry JSON URL to add before running; repeat "
        "the flag for each one (spotiflac --registries URL1 --registries "
        "URL2 URL DEST). Equivalent to SPOTIFLAC_REGISTRIES or adding them "
        "from the Interactive/GUI registry manager. Persisted to "
        "~/.spotiflac/registry_settings.json, so you only need to pass this "
        "once — subsequent runs pick it up automatically. Must be https://. "
        "See Extensions in the README.",
    )
    parser.add_argument(
        "--registry-directories",
        action="append",
        default=None,
        dest="registry_directories",
        metavar="URL",
        help="A directory JSON URL to add before running — a directory "
        "lists *registries* (for you to review and add yourself), rather "
        "than extensions directly; repeat the flag for each one. Equivalent "
        "to SPOTIFLAC_REGISTRY_DIRECTORIES or adding one from the GUI's "
        "Discover screen. Persisted to ~/.spotiflac/directory_settings.json. "
        "Must be https://. See Extensions in the README.",
    )
    parser.add_argument("--loop", "-l", type=int, default=pd.get("loop", None))
    parser.add_argument(
        "--watch",
        type=int,
        default=pd.get("watch", None),
        metavar="MINUTES",
        help="Re-run this exact command every MINUTES, forever (until "
        "interrupted), instead of exiting after one pass. Since already-"
        "downloaded tracks are always skipped (by ISRC/tags for --playlist, "
        "by filename otherwise), each cycle after the first only fetches "
        "what's new — a simple way to keep a playlist/album/artist folder "
        "in sync without a separate scheduler. Unlike --loop (which retries "
        "*failed* tracks for a bounded time after one session), --watch "
        "re-runs the whole sync indefinitely. Combine both if you want "
        "each cycle to also retry transient failures. Not available in "
        "--interactive mode. Does NOT cover Spotify 'Liked Songs' — that "
        "needs an authenticated Spotify session, which this project "
        "deliberately doesn't implement (see the 'no-account' design goal "
        "in the README); point --watch at a public playlist/album/artist "
        "URL instead.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=pd.get("verbose", False),
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=pd.get("interactive", False),
        help="Launch interactive mode (wizard)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        default=False,
        help="Launch graphical user interface (GUI)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        default=False,
        help="Launch the GUI as a local web server instead of a native window "
        "(same interface, open it at http://<host>:<port> in a browser)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind --web to. Defaults to 127.0.0.1 (this machine only). "
        "Binding to 0.0.0.0 or a LAN address exposes the GUI — including "
        "download-triggering endpoints — to anyone who can reach it, with "
        "no authentication. Only do this deliberately.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind --web to (default: 8000)",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        dest="health_check",
        default=False,
        help="Probe direct lyrics-provider servers for reachability and exit "
        "(no download). Optionally narrow the check with --lyrics-providers.",
    )

    # ── Profile ─────────────────────────────────────────────────────────────
    trust_grp = parser.add_argument_group("Extension trust")
    trust_grp.add_argument(
        "--min-trust-tier",
        choices=list(TRUST_TIERS),
        default=None,
        metavar="TIER",
        help="Refuse registry extensions below this assurance level: "
        "'unverified' (default — install anything, as before), "
        "'checksum-only' (registry must publish a sha256), or 'signed' "
        "(entry must carry an Ed25519 signature that verifies against a key "
        "you added with --trust-key-add). Falls back to $SPOTIFLAC_MIN_TRUST. "
        "Does not apply to extensions you install from a local file.",
    )

    profile_grp = parser.add_argument_group("Profile")
    profile_grp.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help="Load a saved profile (overrides config.json defaults, CLI flags take precedence)",
    )
    profile_grp.add_argument(
        "--save-profile",
        default=None,
        dest="save_profile",
        metavar="NAME",
        help="Save the current configuration as a named profile after the run",
    )

    # ── Lyrics ──────────────────────────────────────────────────────────────
    lyrics_grp = parser.add_argument_group("Lyrics")
    lyrics_grp.add_argument(
        "--no-lyrics",
        action="store_false",
        dest="embed_lyrics",
        help="Disable lyrics embedding (enabled by default)",
    )
    parser.set_defaults(embed_lyrics=pd.get("embed_lyrics", True))
    lyrics_grp.add_argument(
        "--lyrics-providers",
        nargs="+",
        default=pd.get("lyrics_providers", ["apple", "lrclib"]),
        dest="lyrics_providers",
        choices=[
            "spotify",
            "apple",
            "deezer",
            "genius",
            "netease",
            "qq",
            "youtube",
            "kugou",
            "musixmatch",
            "amazon",
            "lrclib",
        ],
    )

    # ── Metadata enrichment ─────────────────────────────────────────────────
    enrich_grp = parser.add_argument_group("Metadata Enrichment")
    enrich_grp.add_argument(
        "--no-enrich",
        action="store_false",
        dest="enrich",
        help="Disable metadata enrichment (enabled by default)",
    )
    parser.set_defaults(enrich=pd.get("enrich_metadata", True))
    enrich_grp.add_argument(
        "--enrich-providers",
        nargs="+",
        default=pd.get(
            "enrich_providers",
            ["deezer", "apple", "qobuz", "tidal"],
        ),
        dest="enrich_providers",
        # SoundCloud isn't in the default above but stays a valid choice —
        # pass it explicitly (--enrich-providers deezer apple qobuz tidal
        # soundcloud) if you want it.
        choices=["deezer", "apple", "qobuz", "tidal", "soundcloud"],
    )

    # ── Transcoding ──────────────────────────────────────────────────────────
    transcode_grp = parser.add_argument_group("Transcoding")
    transcode_grp.add_argument(
        "--transcode",
        choices=["none", "mp3"],
        default=pd.get("transcode_to") or "none",
        dest="transcode_to",
        help="Convert every downloaded track to this format (default: none — "
        "keep the provider's original format). Requires ffmpeg. Tracks already "
        "present in the target format are skipped without contacting a provider.",
    )
    transcode_grp.add_argument(
        "--mp3",
        action="store_const",
        const="mp3",
        dest="transcode_to",
        help="Shorthand for --transcode mp3 (320 kbps unless --transcode-bitrate is given)",
    )
    transcode_grp.add_argument(
        "--transcode-bitrate",
        default=pd.get("transcode_bitrate", "320k"),
        dest="transcode_bitrate",
        metavar="RATE",
        help="Bitrate for --transcode (default: 320k)",
    )
    transcode_grp.add_argument(
        "--keep-original",
        action="store_true",
        dest="transcode_keep_original",
        default=pd.get("transcode_keep_original", False),
        help="Keep the original lossless file next to the transcoded one "
        "(default: the source is deleted after a successful conversion)",
    )

    # ── Quality Verification ────────────────────────────────────────────────
    verify_grp = parser.add_argument_group("Quality Verification")
    verify_grp.add_argument(
        "--verify-hires",
        action="store_true",
        dest="verify_hires",
        default=pd.get("verify_hires", False),
        help="After each successful lossless download, run a spectral "
        "analysis to flag files that declare a high sample rate but whose "
        "actual content stops at standard-definition frequencies (a common "
        "sign of upsampling / fake Hi-Res). A finding is only logged as a "
        "warning — it never fails or removes the download. Off by default: "
        "requires the optional 'librosa'/'numpy' dependencies "
        "(pip install SpotiFLAC[hires]) and adds a few seconds of analysis "
        "per track. Skipped automatically for lossy formats (e.g. --mp3).",
    )

    # ── Retry ────────────────────────────────────────────────────────────────
    library_grp = parser.add_argument_group("Music library")
    library_grp.add_argument(
        "--library-rescan",
        dest="library_type",
        choices=list(LIBRARY_SUPPORTED),
        default=None,
        metavar="TYPE",
        help="Ask a music server to rescan once the run finishes, so new "
        "files show up without waiting for its own scheduled scan. "
        "Requires --library-url.",
    )
    library_grp.add_argument(
        "--library-url",
        dest="library_url",
        default=None,
        metavar="URL",
        help="Base URL of the music server, e.g. http://nas.local:8096",
    )
    library_grp.add_argument(
        "--library-token",
        dest="library_token",
        default=None,
        metavar="TOKEN",
        help=f"API token (Plex/Jellyfin/Emby) or password "
        f"(Navidrome/Subsonic). Falls back to ${LIBRARY_TOKEN_ENV}.",
    )
    library_grp.add_argument(
        "--library-user",
        dest="library_user",
        default=None,
        metavar="USERNAME",
        help=f"Username, for Navidrome/Subsonic only. Falls back to "
        f"${LIBRARY_USER_ENV}.",
    )
    library_grp.add_argument(
        "--write-m3u",
        dest="write_m3u",
        default=None,
        metavar="PATH",
        help="Write an extended M3U of everything downloaded in this run. "
        "Paths are relative to the playlist file, so the folder stays "
        "portable.",
    )

    output_grp = parser.add_argument_group("Output")
    output_grp.add_argument(
        "--json",
        dest="json_report",
        action="store_true",
        help="Print a machine-readable report of the run to stdout when it "
        "finishes: one record per track with status, provider, format, path "
        "and error. Human-readable output already goes to stderr, so "
        "`spotiflac ... --json | jq` works unchanged.",
    )

    hooks_grp = parser.add_argument_group("Post-download hooks")
    hooks_grp.add_argument(
        "--post-hook",
        dest="post_hooks",
        action="append",
        default=pd.get("post_download_hooks", None),
        metavar="MODULE:FUNCTION",
        help="Call your own Python after every finished track, with the "
        "DownloadResult and TrackMetadata as objects — e.g. "
        "'mylib.hooks:on_track'. Repeatable. The typed alternative to "
        "--post-action=command; see SpotiFLAC/core/hooks.py.",
    )

    resume_grp = parser.add_argument_group("Resume")
    resume_grp.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        default=pd.get("resume", True),
        help="Restart every interrupted download from zero instead of "
        "continuing it, and delete leftover .part files at the end of the "
        "run. Resuming is on by default.",
    )

    retry_grp = parser.add_argument_group("Retry")
    retry_grp.add_argument(
        "--retries",
        type=int,
        default=pd.get("track_max_retries", 0),
        dest="retries",
        metavar="N",
        help="Extra download attempts per track on failure (default: 0). "
        "Retries cycle through all providers with exponential backoff (2s, 4s, 8s…).",
    )

    # ── Concurrency ──────────────────────────────────────────────────────────
    concurrency_grp = parser.add_argument_group("Concurrency")
    concurrency_grp.add_argument(
        "--max-concurrent",
        type=int,
        default=pd.get("max_concurrent_downloads", 2),
        dest="max_concurrent",
        metavar="N",
        help="How many tracks to download at once (default: 2). Each one still "
        "tries its providers in order/fallback on its own — this only "
        "controls how many tracks run at the same time. Use 1 for fully "
        "sequential downloads with no interleaved console output.",
    )

    # ── Timeout ──────────────────────────────────────────────────────────────
    timeout_grp = parser.add_argument_group("Timeout")
    timeout_grp.add_argument(
        "--timeout",
        type=int,
        default=pd.get("timeout_s", 180),
        dest="timeout_s",
        metavar="SECONDS",
        help="Maximum seconds allowed for each provider attempt (default: 180). "
        "Setting 0 disables the timeout. "
        "The next provider is tried when the timeout expires.",
    )

    # ── Post-download ─────────────────────────────────────────────────────────
    post_grp = parser.add_argument_group("Post-Download")
    post_grp.add_argument(
        "--post-action",
        choices=["none", "open_folder", "notify", "command"],
        default=pd.get("post_download_action", "none"),
        dest="post_action",
        help="Action to perform after all downloads finish (default: none)",
    )
    post_grp.add_argument(
        "--post-command",
        default=pd.get("post_download_command", ""),
        dest="post_command",
        metavar="CMD",
        help="Shell command for --post-action=command. "
        "Placeholders: {folder} {succeeded} {failed}",
    )

    return parser.parse_args()


async def watch_forever(
    run_once: Callable[[], Awaitable[None]],
    minutes: int,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Calls `run_once()` again every `minutes`, forever, until interrupted.

    Backs `--watch`: every download path it's used with (a single URL, or
    --playlist multi-sync) already indexes what's on disk and skips
    existing tracks — see BaseProvider._file_exists() and
    playlist_sync.find_existing_track() — so each cycle after the first is
    cheap, only fetching what's actually new. `run_once` has already run
    once (the plain, --watch-less pass) by the time this is called; this
    only owns the *repeat* — never returns on its own.

    `sleep` is overridable so tests can drive this without a real delay.
    """
    unit = "minute" if minutes == 1 else "minutes"
    while True:
        print(
            f"[watch] Sync complete. Sleeping {minutes} {unit} "
            f"before the next pass — Ctrl+C to stop.",
        )
        await sleep(minutes * 60)
        print("[watch] Re-syncing now…")
        await run_once()


async def _write_run_m3u_async(report, destination: str) -> None:
    """Writes an M3U of everything the run downloaded. Never raises."""
    from pathlib import Path

    from .core.playlist_sync import write_if_changed_async

    logger = logging.getLogger("SpotiFLAC")
    paths = report.downloaded_paths()
    if not paths:
        logger.info("[m3u] Nothing was downloaded; no playlist written")
        return
    try:
        target = Path(destination).expanduser()
        await write_if_changed_async(target, report.to_m3u(target))
        logger.info("[m3u] Wrote %d track(s) to %s", len(paths), target)
    except Exception as exc:
        # The files are on disk regardless; a playlist that could not be
        # written is not a reason to report the run as failed.
        logger.warning("[m3u] Could not write %s: %s", destination, exc)


async def _notify_library_async(
    kind: str,
    url: str,
    token: str | None,
    username: str | None,
) -> None:
    """Asks a music server to rescan. Never raises."""
    from .core.library_notify import LibraryNotifyError, build_target, request_rescan

    logger = logging.getLogger("SpotiFLAC")
    try:
        target = build_target(kind, url, token, username)
    except LibraryNotifyError as exc:
        # A configuration mistake, so say so plainly rather than logging a
        # connection error the user would then go and debug on the server.
        logger.error("[library] %s", exc)
        return
    await request_rescan(target)


async def _run_download_async(
    url: str | list[str],
    *,
    output_dir: str,
    services: list[str],
    filename_format: str,
    use_track_numbers: bool,
    use_album_track_numbers: bool,
    use_artist_subfolders: bool,
    use_album_subfolders: bool,
    create_playlist_subfolders: bool,
    loop: int | None,
    quality: str,
    first_artist_only: bool,
    artist_separator: str | None = None,
    include_featuring: bool,
    log_level: int,
    output_path: str | None,
    allow_fallback: bool,
    embed_lyrics: bool,
    lyrics_providers: list[str],
    enrich_metadata: bool,
    enrich_providers: list[str],
    qobuz_local_api_url: str | None,
    tidal_custom_api: str | None,
    track_max_retries: int,
    post_download_action: str,
    post_download_command: str,
    timeout_s: int | None,
    transcode_to: str | None = None,
    transcode_bitrate: str = "320k",
    transcode_keep_original: bool = False,
    playlist_urls: list[str] | None = None,
    m3u_format: str = "m3u8",
    max_concurrent_downloads: int = 2,
    verify_hires: bool = False,
    resume: bool = True,
    post_download_hooks: list[str] | None = None,
    json_report: bool = False,
    library_type: str | None = None,
    library_url: str | None = None,
    library_token: str | None = None,
    library_user: str | None = None,
    write_m3u: str | None = None,
) -> None:
    """Async bridge to SpotiflacDownloader, bypassing the synchronous
    `SpotiFLAC()` wrapper (which would do a nested `asyncio.run()` and
    fallirebbe).
    """
    logger = logging.getLogger("SpotiFLAC")
    if not logger.handlers:
        # stderr under --json: stdout carries the report document and
        # nothing else, so the output stays pipeable into jq.
        handler = logging.StreamHandler(sys.stderr if json_report else sys.stdout)
        handler.setFormatter(
            _CleanConsoleFormatter("[%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(log_level)

    # The report is just another post-download hook, so it observes exactly
    # the same per-track events a user-supplied --post-hook does.
    # The report backs three separate features now (--json, --write-m3u, and
    # the count in the library-rescan log line), so it is built whenever any
    # of them is on rather than for --json alone.
    report = RunReport() if (json_report or write_m3u) else None
    hooks: list = list(post_download_hooks or [])
    if report is not None:
        hooks.append(report)

    opts = DownloadOptions(
        output_dir=output_dir,
        services=services,
        filename_format=filename_format,
        use_track_numbers=use_track_numbers,
        use_album_track_numbers=use_album_track_numbers,
        use_artist_subfolders=use_artist_subfolders,
        allow_fallback=allow_fallback,
        use_album_subfolders=use_album_subfolders,
        create_playlist_subfolders=create_playlist_subfolders,
        quality=quality,
        first_artist_only=first_artist_only,
        artist_separator=artist_separator,
        output_path=output_path,
        embed_lyrics=embed_lyrics,
        lyrics_providers=lyrics_providers,
        enrich_metadata=enrich_metadata,
        enrich_providers=enrich_providers,
        qobuz_local_api_url=qobuz_local_api_url,
        track_max_retries=track_max_retries,
        post_download_action=post_download_action,
        post_download_command=post_download_command,
        tidal_custom_api=tidal_custom_api,
        timeout_s=timeout_s,
        transcode_to=transcode_to,
        transcode_bitrate=transcode_bitrate,
        transcode_keep_original=transcode_keep_original,
        max_concurrent_downloads=max(1, max_concurrent_downloads),
        verify_hires=verify_hires,
        resume=resume,
        post_download_hooks=hooks,
    )

    try:
        downloader = SpotiflacDownloader(opts)
        if playlist_urls:
            if loop:
                logger.warning(
                    "--loop is ignored with --playlist: run the command again "
                    "to sync the playlists.",
                )
            await downloader.run_playlists_async(playlist_urls, m3u_format=m3u_format)
        else:
            await downloader.run_async(url, loop_minutes=loop)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger("SpotiFLAC").exception(
            "Critical error during execution: %s",
            e,
        )
    finally:
        # All three run in `finally` so an interrupted run still writes the
        # playlist for what it did fetch, still tells the library server, and
        # still emits a valid document. A script parsing this should never
        # have to distinguish "no JSON" from "no tracks".
        if write_m3u and report is not None:
            await _write_run_m3u_async(report, write_m3u)

        if library_type and library_url:
            await _notify_library_async(
                library_type, library_url, library_token, library_user
            )

        if json_report and report is not None:
            print(report.to_json(), flush=True)


def _split_positionals(args: argparse.Namespace) -> tuple[list[str], str | None]:
    """Splits the positional arguments between playlist URLs and destination.

    Without ``--playlist`` nothing changes: ``URL DEST``. With it the
    destination is the only positional the user has to give
    (``spotiflac -p URL1 -p URL2 DEST``), while a first playlist passed
    positionally (``spotiflac URL DEST -p URL2``) still works.
    """
    playlists = list(args.playlists or [])
    if not playlists:
        return [], args.output_dir
    if args.url and args.output_dir:
        playlists.insert(0, args.url)
        return playlists, args.output_dir
    return playlists, args.output_dir or args.url


async def amain() -> None:
    """Coordinate GUI, interactive, and command-line execution for SpotiFLAC.

    Handles startup checks, extension installation, configuration loading, profile management, argument parsing, and download execution across the supported application modes.
    """
    from .core.ffmpeg_check import print_ffmpeg_warning
    from .core.node_check import print_node_warning

    _print_welcome_banner()

    with contextlib.suppress(Exception):
        await check_for_updates_async()

    _register_cli_registries(_early_registries_from_argv())
    _register_cli_registry_directories(_early_registry_directories_from_argv())

    try:
        from .extensions.manager import ExtensionManager

        await asyncio.to_thread(
            ExtensionManager,
            auto_install_downloads=True,
            min_trust_tier=_early_min_trust_from_argv(),
        )
    except Exception:
        pass

    if "--gui" in sys.argv:
        from .app import run_gui

        run_gui()
        return

    if "--web" in sys.argv:
        # --host/--port need the parsed args (argparse), not a raw sys.argv
        # scan like the flags above, since they take a value.
        # allow_abbrev=False is load-bearing, not tidiness: argparse expands
        # unambiguous prefixes by default, and `--web` — the flag that got us
        # into this branch and is therefore always present — is a prefix of
        # both --web-token and --web-multiuser. With abbreviation on, argparse
        # calls that ambiguous and exits(2), so plain `spotiflac --web` could
        # never start the server at all.
        web_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        web_parser.add_argument("--host", default="127.0.0.1")
        web_parser.add_argument("--port", type=int, default=8000)
        web_parser.add_argument("--web-token", dest="token", default=None)
        web_parser.add_argument(
            "--web-multiuser", dest="multiuser", action="store_true"
        )
        web_args, _ = web_parser.parse_known_args(sys.argv[1:])

        try:
            from .webapp import resolve_web_token
            from .webapp import run_async as run_web
        except ImportError as exc:
            # webapp.py imports FastAPI at module level, and FastAPI/uvicorn
            # ship in the optional `web` extra rather than the base install —
            # web mode is one of several, and most users of the module never
            # start a server. Say which command fixes it instead of handing
            # over a bare traceback.
            print(
                f"--web needs the web extra, which isn't installed ({exc}).\n"
                "Install it with:\n"
                "    pip install 'SpotiFLAC[web]'",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc

        await run_web(
            host=web_args.host,
            port=web_args.port,
            token=resolve_web_token(web_args.token),
            multiuser=web_args.multiuser,
        )
        return

    if "--web-user-add" in sys.argv:
        user_parser = argparse.ArgumentParser(add_help=False)
        user_parser.add_argument(
            "--web-user-add", dest="creds", nargs=2, metavar=("USERNAME", "PASSWORD")
        )
        user_args, _ = user_parser.parse_known_args(sys.argv[1:])

        from .core.web_users import WebUserError, create_user

        try:
            create_user(*user_args.creds)
        except WebUserError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Web user '{user_args.creds[0]}' created.")
        return

    if "--web-user-remove" in sys.argv:
        user_rm_parser = argparse.ArgumentParser(add_help=False)
        user_rm_parser.add_argument(
            "--web-user-remove", dest="username", metavar="USERNAME"
        )
        user_rm_args, _ = user_rm_parser.parse_known_args(sys.argv[1:])

        from .core.web_users import delete_user

        found = delete_user(user_rm_args.username)
        print(
            f"Web user '{user_rm_args.username}' removed."
            if found
            else f"No web user named '{user_rm_args.username}'."
        )
        return

    if any(
        flag in sys.argv for flag in ("--cache-stats", "--cache-prune", "--cache-clear")
    ):
        from .core import cache_admin

        cache_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        cache_parser.add_argument("--cache-stats", action="store_true")
        cache_parser.add_argument("--cache-prune", action="store_true")
        cache_parser.add_argument("--cache-clear", action="store_true")
        cache_parser.add_argument(
            "--cache-max-age-days",
            type=float,
            default=cache_admin.DEFAULT_MAX_AGE_S / 86400,
        )
        cache_parser.add_argument("--dry-run", action="store_true")
        cache_parser.add_argument("--json", dest="as_json", action="store_true")
        cache_args, _ = cache_parser.parse_known_args(sys.argv[1:])

        if cache_args.cache_clear:
            result = cache_admin.clear(dry_run=cache_args.dry_run)
            if cache_args.as_json:
                print(cache_admin.to_json(result))
            else:
                verb = "Would remove" if result["dry_run"] else "Removed"
                items = ", ".join(result["removed"]) or "nothing"
                print(f"{verb}: {items}")
                print(f"Freed: {cache_admin.human_bytes(result['freed_bytes'])}")
                if result["preserved"]:
                    print(f"Kept (configuration): {', '.join(result['preserved'])}")
        elif cache_args.cache_prune:
            # --cache-max-age-days is a float, so argparse happily accepts
            # -1, nan and inf; prune() rejects them rather than silently
            # deleting everything or nothing. A bad flag value is a usage
            # error, so report it the way argparse reports every other one
            # (message on stderr, exit 2) instead of a traceback.
            try:
                result = cache_admin.prune(
                    max_age_s=cache_args.cache_max_age_days * 86400,
                    dry_run=cache_args.dry_run,
                )
            except ValueError as exc:
                cache_parser.error(str(exc))
            if cache_args.as_json:
                print(cache_admin.to_json(result))
            else:
                verb = "Would remove" if result["dry_run"] else "Removed"
                print(
                    f"{verb} {result['removed_files']} cached response(s), "
                    f"{cache_admin.human_bytes(result['freed_bytes'])}"
                )
        else:
            data = cache_admin.stats()
            print(
                cache_admin.to_json(data)
                if cache_args.as_json
                else cache_admin.format_stats(data)
            )
        return

    if "--web-user-list" in sys.argv:
        from .core.web_users import list_usernames

        users = list_usernames()
        print("\n".join(users) if users else "No web users configured.")
        return

    if "--health-check" in sys.argv:
        # --lyrics-providers narrows which servers get probed, same as for
        # --host/--port above: needs the parsed value, not a raw sys.argv scan.
        hc_parser = argparse.ArgumentParser(add_help=False)
        hc_parser.add_argument("--lyrics-providers", nargs="+", default=None)
        hc_args, _ = hc_parser.parse_known_args(sys.argv[1:])

        from .core.health_check import print_health_report, run_health_check

        results = await run_health_check(hc_args.lyrics_providers)
        print_health_report(results)
        if not all(r.ok for r in results):
            sys.exit(1)
        return

    if "--ext-scaffold" in sys.argv:
        scaffold_parser = argparse.ArgumentParser(add_help=False)
        scaffold_parser.add_argument("--ext-scaffold", dest="name", required=True)
        scaffold_parser.add_argument(
            "--runtime", choices=["python", "javascript"], default="python"
        )
        scaffold_parser.add_argument("--output-dir", default=None)
        scaffold_args, _ = scaffold_parser.parse_known_args(sys.argv[1:])

        from .tools.ext_scaffold import scaffold_extension

        try:
            target = scaffold_extension(
                scaffold_args.name,
                runtime=scaffold_args.runtime,
                output_dir=scaffold_args.output_dir,
            )
        except (ValueError, FileExistsError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Created {scaffold_args.runtime} extension skeleton at: {target}")
        print(f"Next: see {target / 'README.md'}")
        return

    if "--ext-dry-run" in sys.argv:
        dryrun_parser = argparse.ArgumentParser(add_help=False)
        dryrun_parser.add_argument("--ext-dry-run", dest="path", required=True)
        dryrun_args, _ = dryrun_parser.parse_known_args(sys.argv[1:])

        from .tools.ext_dryrun import dry_run

        report = dry_run(dryrun_args.path)
        print(report.summary())
        if not report.passed:
            sys.exit(1)
        return

    if "--trust-key-add" in sys.argv:
        trust_parser = argparse.ArgumentParser(add_help=False)
        trust_parser.add_argument(
            "--trust-key-add", dest="key", nargs=2, metavar=("NAME", "PUBLIC_KEY_B64")
        )
        trust_args, _ = trust_parser.parse_known_args(sys.argv[1:])

        from .extensions.trust import TrustKeyError, add_trusted_key

        try:
            add_trusted_key(*trust_args.key)
        except TrustKeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Trusted key '{trust_args.key[0]}' added.")
        return

    if "--trust-key-remove" in sys.argv:
        trust_rm_parser = argparse.ArgumentParser(add_help=False)
        trust_rm_parser.add_argument("--trust-key-remove", dest="name", metavar="NAME")
        trust_rm_args, _ = trust_rm_parser.parse_known_args(sys.argv[1:])

        from .extensions.trust import remove_trusted_key

        found = remove_trusted_key(trust_rm_args.name)
        print(
            f"Trusted key '{trust_rm_args.name}' removed."
            if found
            else f"No trusted key named '{trust_rm_args.name}'."
        )
        return

    if "--trust-key-list" in sys.argv:
        from .extensions.trust import list_trusted_keys

        keys = list_trusted_keys()
        if not keys:
            print("No trusted keys configured.")
        for idx, k in enumerate(keys, start=1):
            name = k.get("name", "") or "(unnamed)"
            # Only the human-assigned name is shown; the key material itself
            # is never written to the console. Use trusted_keys.json directly
            # if you need to inspect or export the public key bytes.
            print(f"key-{idx}: {name}")
        return

    if "--interactive" in sys.argv:
        print_ffmpeg_warning()
        print_node_warning()
        cfg = await run_interactive(_early_min_trust_from_argv())

        verbose = (
            cfg.get("verbose", False) or "--verbose" in sys.argv or "-v" in sys.argv
        )
        log_level = _resolve_log_level(verbose)
        _root_handler = logging.StreamHandler(sys.stdout)
        _root_handler.setFormatter(
            _CleanConsoleFormatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
        )
        logging.basicConfig(level=log_level, handlers=[_root_handler])

        async def _run_once() -> None:
            await _run_download_async(
                cfg["url"],
                output_dir=cfg["output_dir"],
                services=cfg["services"],
                filename_format=cfg["filename_format"],
                use_track_numbers=cfg["use_track_numbers"],
                use_album_track_numbers=cfg["use_album_track_numbers"],
                use_artist_subfolders=cfg["use_artist_subfolders"],
                use_album_subfolders=cfg["use_album_subfolders"],
                create_playlist_subfolders=cfg.get("create_playlist_subfolders", True),
                loop=cfg.get("loop"),
                quality=cfg["quality"],
                first_artist_only=cfg["first_artist_only"],
                artist_separator=cfg.get("artist_separator"),
                include_featuring=cfg.get("include_featuring", True),
                log_level=log_level,
                output_path=cfg.get("output_path"),
                allow_fallback=cfg.get("allow_fallback", True),
                embed_lyrics=cfg["embed_lyrics"],
                lyrics_providers=cfg["lyrics_providers"],
                enrich_metadata=cfg["enrich_metadata"],
                enrich_providers=cfg["enrich_providers"],
                qobuz_local_api_url=cfg.get("qobuz_local_api_url"),
                tidal_custom_api=cfg.get("tidal_custom_api") or None,
                track_max_retries=cfg.get("track_max_retries", 0),
                post_download_action=cfg.get("post_download_action", "none"),
                post_download_command=cfg.get("post_download_command", ""),
                resume=cfg.get("resume", True),
                post_download_hooks=cfg.get("post_download_hooks", []),
                # These are CLI-only flags, and `args` does not exist yet on
                # this path — it is parsed further down, in the branch this
                # one returns before reaching, so reading it here raised
                # NameError as soon as an interactive run started
                # downloading. The wizard does not ask about any of them, so
                # the interactive defaults are simply "off"; cfg.get() leaves
                # room for it to start asking.
                json_report=cfg.get("json_report", False),
                library_type=cfg.get("library_type"),
                library_url=cfg.get("library_url"),
                library_token=cfg.get("library_token"),
                library_user=cfg.get("library_user"),
                write_m3u=cfg.get("write_m3u"),
                timeout_s=cfg.get("timeout_s"),
                transcode_to=cfg.get("transcode_to"),
                transcode_bitrate=cfg.get("transcode_bitrate", "320k"),
                transcode_keep_original=cfg.get("transcode_keep_original", False),
                max_concurrent_downloads=cfg.get("max_concurrent_downloads", 2),
                verify_hires=cfg.get("verify_hires", False),
            )

        await _run_once()

        if cfg.get("watch"):
            await watch_forever(_run_once, cfg["watch"])
        return

    if len(sys.argv) == 1:
        parser = argparse.ArgumentParser(
            prog="spotiflac",
            description="Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube, Pandora and more.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument(
            "--gui",
            action="store_true",
            help="Launch graphical user interface (GUI)",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Launch interactive mode (wizard)",
        )
        parser.print_help()
        return

    print_ffmpeg_warning()
    print_node_warning()
    profile_defaults: dict = {}
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        if idx + 1 < len(sys.argv):
            profile_defaults = await _load_profile_into_defaults(sys.argv[idx + 1])

    file_cfg = load_config()
    merged_defaults = {**file_cfg, **profile_defaults}

    args = parse_args(profile_defaults=merged_defaults)
    playlist_urls, output_dir = _split_positionals(args)

    if not (args.url or playlist_urls) or not output_dir:
        parser = argparse.ArgumentParser(
            prog="spotiflac",
            description="Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube, Pandora and more.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument(
            "--gui",
            action="store_true",
            help="Launch graphical user interface (GUI)",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Launch interactive mode (wizard)",
        )
        parser.print_help()
        return

    quality = args.quality or merged_defaults.get("quality", "LOSSLESS")
    qobuz_local_api_url = args.qobuz_local_api_url or merged_defaults.get(
        "qobuz_local_api_url",
    )
    tidal_custom_api = args.tidal_custom_api or merged_defaults.get("tidal_custom_api")
    timeout_s = (
        args.timeout_s
        if args.timeout_s is not None
        else merged_defaults.get("timeout_s")
    )
    track_max_retries = (
        args.retries
        if args.retries is not None
        else merged_defaults.get("track_max_retries", 0)
    )

    log_level = _resolve_log_level(args.verbose)
    log_format = (
        "%(levelname)s:%(name)s: %(message)s"
        if args.verbose
        else "%(levelname)s: %(message)s"
    )
    _cli_handler = logging.StreamHandler(sys.stdout)
    _cli_handler.setFormatter(_CleanConsoleFormatter(log_format))
    logging.basicConfig(level=log_level, handlers=[_cli_handler])

    async def _run_once() -> None:
        await _run_download_async(
            args.url or "",
            output_dir=output_dir,
            services=args.service,
            filename_format=args.filename_format,
            use_track_numbers=args.use_track_numbers,
            use_album_track_numbers=args.use_album_track_numbers,
            use_artist_subfolders=args.use_artist_subfolders,
            use_album_subfolders=args.use_album_subfolders,
            create_playlist_subfolders=args.create_playlist_subfolders,
            loop=args.loop,
            quality=quality,
            first_artist_only=args.first_artist_only,
            artist_separator=args.artist_separator,
            include_featuring=args.include_featuring,
            log_level=log_level,
            output_path=args.output_path,
            allow_fallback=True,
            embed_lyrics=args.embed_lyrics,
            lyrics_providers=args.lyrics_providers,
            enrich_metadata=args.enrich,
            enrich_providers=args.enrich_providers,
            qobuz_local_api_url=qobuz_local_api_url,
            tidal_custom_api=tidal_custom_api,
            track_max_retries=track_max_retries,
            post_download_action=args.post_action,
            post_download_command=args.post_command,
            resume=args.resume,
            post_download_hooks=args.post_hooks or [],
            json_report=args.json_report,
            library_type=args.library_type,
            library_url=args.library_url,
            library_token=args.library_token,
            library_user=args.library_user,
            write_m3u=args.write_m3u,
            timeout_s=timeout_s,
            transcode_to=args.transcode_to,
            transcode_bitrate=args.transcode_bitrate,
            transcode_keep_original=args.transcode_keep_original,
            playlist_urls=playlist_urls,
            m3u_format=args.m3u_format,
            max_concurrent_downloads=args.max_concurrent,
            verify_hires=args.verify_hires,
        )

    await _run_once()

    if args.save_profile:
        try:
            from .core.profiles import save_profile_async

            profile_cfg = {
                "output_dir": args.output_dir or "./Downloads",
                "services": args.service,
                "quality": quality,
                "filename_format": args.filename_format,
                "use_track_numbers": args.use_track_numbers,
                "use_album_track_numbers": args.use_album_track_numbers,
                "use_artist_subfolders": args.use_artist_subfolders,
                "use_album_subfolders": args.use_album_subfolders,
                "create_playlist_subfolders": args.create_playlist_subfolders,
                "first_artist_only": args.first_artist_only,
                "artist_separator": args.artist_separator,
                "include_featuring": args.include_featuring,
                "allow_fallback": True,
                "embed_lyrics": args.embed_lyrics,
                "lyrics_providers": args.lyrics_providers,
                "enrich_metadata": args.enrich,
                "enrich_providers": args.enrich_providers,
                "transcode_to": args.transcode_to,
                "transcode_bitrate": args.transcode_bitrate,
                "transcode_keep_original": args.transcode_keep_original,
                "m3u_format": args.m3u_format,
                "track_max_retries": track_max_retries,
                "post_download_action": args.post_action,
                "post_download_command": args.post_command,
                "resume": args.resume,
                "post_download_hooks": args.post_hooks or [],
                "qobuz_local_api_url": qobuz_local_api_url,
                "tidal_custom_api": tidal_custom_api,
                "timeout_s": timeout_s,
                "loop": args.loop,
                "watch": args.watch,
                "max_concurrent_downloads": args.max_concurrent,
                "verify_hires": args.verify_hires,
            }
            await save_profile_async(args.save_profile, profile_cfg)
        except Exception:
            pass

    if args.watch:
        await watch_forever(_run_once, args.watch)


def main() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()

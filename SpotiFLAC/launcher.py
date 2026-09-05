#!/usr/bin/env python3
"""CLI entry point for SpotiFLAC.

=== Async migration ===
The entire entry point now runs on a single shared event loop instead of
delegating to `SpotiFLAC(...)` (a sync wrapper that opens its own
asyncio.run() internally). It uses `SpotiflacDownloader` directly, which is
already 100% async-native, and that is why `check_for_updates_async` and the
guided modes can now be awaited in the same loop instead of opening a new
one each time.

=== Guided mode ===
`--tui` is it. `--interactive` still parses, warns, and opens the same
screen; `SpotiFLAC/interactive.py` is no longer reachable from here and goes
away a release from now. A terminal that cannot host the TUI should use the
command line, which is better suited to that anyway — there is deliberately
no second wizard to fall back to.
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
import time
import warnings
from collections.abc import Awaitable, Callable

from .check_update import check_for_updates_async
from .client import _CleanConsoleFormatter
from .core.library_notify import LIBRARY_TOKEN_ENV, LIBRARY_USER_ENV
from .core.library_notify import SUPPORTED as LIBRARY_SUPPORTED
from .core.notifiers import EVENTS as NOTIFY_EVENTS
from .core.notifiers import KINDS as NOTIFY_KINDS
from .core.notifiers import NOTIFY_TOKEN_ENV, NOTIFY_URL_ENV
from .core.output_sink import sink_active
from .core.report import RunReport
from .core.transcode import LOSSLESS_FORMATS, SUPPORTED_FORMATS
from .downloader import DownloadOptions, SpotiflacDownloader
from .core.web_users import ROLES as WEB_USER_ROLES
from .extensions.trust import TRUST_TIERS


def _match_score(value: str) -> float:
    """A --csv-min-score argparse type: a real number in 0…1.

    `type=float` alone accepts "-1", "5" and "nan". None of them mean
    anything as a threshold, and each fails in its own quiet way: a negative
    or a NaN makes every comparison against it behave as though no floor
    were set, so an export of messy titles downloads a wrong match under the
    right filename — which is precisely what the flag exists to prevent.
    """
    try:
        score = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    # NaN fails both comparisons below, so it is rejected by the same test.
    if not 0.0 <= score <= 1.0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a match score: expected a number from 0.0 "
            "(accept anything) to 1.0 (accept only an exact match)"
        )
    return score


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


def _argv_has(*flags: str) -> bool:
    """True if any of `flags` is present in argv, bare or in `flag=value` form.

    The subcommand dispatch below routes on raw membership checks; without
    this, a flag that takes a value (`--upgrade-library=/music`) would slip
    past its own handler and fall through to the ordinary download path.
    argparse itself accepts either form, so only the routing check needs it.
    """
    return any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in sys.argv[1:]
        for flag in flags
    )


def _is_help_invocation(argv: list[str] | None = None) -> bool:
    """Whether this run only wants the usage text and will then exit.

    argparse handles -h/--help itself, further down amain(), and everything
    before that point is startup work: an update check and the extension
    registry bootstrap, both of which reach the network. So `spotiflac
    --help` waited on three HTTP attempts per configured registry before
    printing a static string — and with a registry unreachable it appeared
    to hang outright, which is the first thing anyone installing this hits.

    Only -h/--help: there is no --version flag, and a subcommand that looks
    informational but is not would be worse to guess at than to leave alone.
    """
    args = sys.argv[1:] if argv is None else argv
    return any(arg in ("-h", "--help") for arg in args)


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


_LOG_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _coerce_log_level(value: str | int) -> int:
    """Turns a level name, alias or number into a logging constant.

    Accepts what a profile stores (an int) as readily as what a user types
    (`debug`, `WARN`), so the same value survives a save/load round trip.
    """
    if isinstance(value, int):
        return value

    name = str(value).strip().upper()
    if not name:
        msg = "log level cannot be empty"
        raise argparse.ArgumentTypeError(msg)
    if name.isdigit():
        return int(name)

    name = {
        "WARN": "WARNING",
        "ERR": "ERROR",
        "CRIT": "CRITICAL",
        "FATAL": "CRITICAL",
    }.get(name, name)
    resolved = logging.getLevelName(name)
    if not isinstance(resolved, int):
        msg = f"invalid log level {value!r}; choose from {', '.join(_LOG_LEVEL_NAMES)}"
        raise argparse.ArgumentTypeError(msg)
    return resolved


#: Chatty libraries whose own INFO records say nothing about what SpotiFLAC
#: is doing — httpx alone emits a line per request. They are pinned to
#: WARNING so the default level can be INFO and stay readable; --verbose
#: lifts the pin, because a network problem is exactly when their frames
#: are worth reading.
_NOISY_LIBRARY_LOGGERS = ("httpx", "httpcore", "hpack", "urllib3", "asyncio")


def _quiet_noisy_libraries(level: int) -> None:
    """Pins third-party loggers to WARNING unless we are debugging."""
    pinned = logging.NOTSET if level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(pinned)


def _resolve_log_level(
    verbose: bool,
    explicit: str | int | None = None,
    profile_default: str | int | None = None,
) -> int:
    """Report what the run is doing, and no more.

    Precedence: --log-level, then --verbose (a shorthand for DEBUG), then
    whatever a profile stored, then the default. A profile value ranks
    *below* --verbose on purpose: it is a saved preference, and a flag typed
    for this one run has to be able to win over it.

    The default is INFO, not ERROR: the milestones of a download — which
    provider was tried, the ticket, the audio fetch, the transcode — are all
    logged at info, and at ERROR a run that hung showed nothing at all until
    it gave up. The libraries that would flood that level are pinned
    separately, see _quiet_noisy_libraries().
    """
    if explicit is not None:
        return _coerce_log_level(explicit)
    if verbose:
        return logging.DEBUG
    if profile_default is not None:
        return _coerce_log_level(profile_default)
    return logging.INFO


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

    # ── CSV input ───────────────────────────────────────────────────────────
    csv_grp = parser.add_argument_group("CSV")
    csv_grp.add_argument(
        "--csv",
        dest="csv_path",
        default=None,
        metavar="FILE",
        help="Download every track listed in a CSV file "
        "(spotiflac --csv tracks.csv DEST). Rows carrying a link (a Spotify "
        "URL, a spotify:track: URI, or any other supported service URL) are "
        "used as they are; rows carrying only a title/artist — or only an "
        "ISRC — are matched against the catalogue. Exports from Exportify, "
        "Soundiiz, TuneMyMusic and a plain one-link-per-line file are all "
        "understood without configuration: the delimiter is detected and the "
        "columns are matched by name. The whole file is treated as one "
        "playlist (see --playlist): duplicated rows are downloaded once, "
        "tracks already in DEST are skipped, and an M3U named after the file "
        "is written next to them (--m3u none disables it).",
    )
    csv_grp.add_argument(
        "--csv-dry-run",
        dest="csv_dry_run",
        action="store_true",
        help="Resolve the CSV and print what would be downloaded — which row "
        "matched what, and with what confidence — without downloading "
        "anything. Worth doing once on an unfamiliar export.",
    )
    csv_grp.add_argument(
        "--csv-min-score",
        dest="csv_min_score",
        type=_match_score,
        default=None,
        metavar="0..1",
        help="How close a catalogue match has to be before a text-only row "
        "is downloaded (default: 0.62). Raise it if a file is producing "
        "wrong matches, lower it for an export with messy titles — a wrong "
        "match is a file with the right name and the wrong music in it, so "
        "unmatched rows are reported rather than guessed at.",
    )
    csv_grp.add_argument(
        "--csv-unresolved",
        dest="csv_unresolved",
        default=None,
        metavar="FILE",
        help="Write the rows that could not be matched to this CSV, in a "
        "shape you can correct (fix the title, paste a link) and feed "
        "straight back to --csv.",
    )
    csv_grp.add_argument(
        "--csv-concurrency",
        dest="csv_concurrency",
        type=int,
        default=4,
        metavar="N",
        help="How many rows are looked up at a time while resolving the file "
        "(default: 4). Unrelated to --max-concurrent, which governs the "
        "downloads themselves.",
    )
    csv_grp.add_argument(
        "--csv-delimiter",
        dest="csv_delimiter",
        default=None,
        metavar="CHAR",
        help="Column separator, when the automatic detection gets it wrong "
        r"(e.g. --csv-delimiter ';'). Use $'\t' for tab-separated files.",
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
    # A pair rather than a lone --no-fallback: a profile can set
    # allow_fallback=False, and without --fallback there would be no way to
    # turn it back on for a single run.
    parser.add_argument(
        "--fallback",
        action="store_true",
        dest="allow_fallback",
        default=pd.get("allow_fallback", True),
        help="Let a provider serve a lower tier when the requested --quality "
        "is unavailable for a track (default: enabled).",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_false",
        dest="allow_fallback",
        help="Fail a track outright when the requested --quality is not "
        "available, instead of accepting a lower tier.",
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
        "--log-level",
        dest="log_level",
        type=_coerce_log_level,
        default=None,
        metavar="LEVEL",
        help="Console log level: "
        + ", ".join(_LOG_LEVEL_NAMES)
        + ". Overrides --verbose, which is a shorthand for DEBUG. "
        "Default: INFO (the milestones of the run). Third-party libraries "
        "are held at WARNING unless the level is DEBUG.",
    )
    # Kept apart from --log-level's own default so the resolver can tell a
    # saved preference from a flag typed for this run.
    parser.set_defaults(profile_log_level=pd.get("log_level"))
    parser.add_argument(
        "--tui",
        action="store_true",
        default=pd.get("tui", False),
        help="Launch the terminal UI (the guided mode)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=pd.get("interactive", False),
        help="Deprecated alias for --tui",
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
    lyrics_grp.add_argument(
        "--apple-lyrics-line-synced",
        action="store_false",
        dest="apple_lyrics_word_by_word",
        help=(
            "get plain line-synced LRC from the Apple lyrics provider instead "
            "of word-by-word (per-syllable) enhanced LRC"
        ),
    )
    lyrics_grp.set_defaults(
        apple_lyrics_word_by_word=pd.get("apple_lyrics_word_by_word", True),
    )
    lyrics_grp.add_argument(
        "--save-lrc",
        action="store_true",
        default=pd.get("save_lrc", False),
        dest="save_lrc",
        help=(
            "also write the lyrics as an .lrc file next to the track, under "
            "the audio file's own name"
        ),
    )
    lyrics_grp.add_argument(
        "--lrc-dir",
        default=pd.get("lrc_library_dir") or None,
        dest="lrc_library_dir",
        metavar="DIR",
        help=(
            "also collect every lyric into DIR as 'Artist - Title.lrc', the "
            "layout overlay players (LyricsX and the like) look lyrics up by"
        ),
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
        choices=["none", *SUPPORTED_FORMATS],
        default=pd.get("transcode_to") or "none",
        dest="transcode_to",
        help="Convert every downloaded track to this format (default: none — "
        "keep the provider's original format). Lossless targets ("
        + ", ".join(LOSSLESS_FORMATS)
        + ") re-encode the samples untouched — same rate, same bit depth; "
        "'mp3' encodes at --transcode-bitrate. ALAC lands in a .m4a and "
        "wavpack in a .wv. Requires ffmpeg. Tracks already present in the "
        "target format are skipped without contacting a provider.",
    )
    transcode_grp.add_argument(
        "--mp3",
        action="store_const",
        const="mp3",
        dest="transcode_to",
        help="Shorthand for --transcode mp3 (320 kbps unless --transcode-bitrate is given)",
    )
    transcode_grp.add_argument(
        "--alac",
        action="store_const",
        const="alac",
        dest="transcode_to",
        help="Shorthand for --transcode alac — lossless .m4a, the format "
        "macOS reads natively (Finder artwork, Music.app, QuickLook)",
    )
    transcode_grp.add_argument(
        "--transcode-bitrate",
        default=pd.get("transcode_bitrate", "320k"),
        dest="transcode_bitrate",
        metavar="RATE",
        help="Bitrate for the lossy --transcode targets (default: 320k). "
        "Ignored by the lossless ones.",
    )
    transcode_grp.add_argument(
        "--keep-original",
        action="store_true",
        dest="transcode_keep_original",
        default=pd.get("transcode_keep_original", False),
        help="Keep the original downloaded file next to the transcoded one "
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

    notify_grp = parser.add_argument_group("Notifications")
    notify_grp.add_argument(
        "--notify",
        dest="notify",
        choices=list(NOTIFY_KINDS),
        default=None,
        help="Send run results to a webhook, Discord, Telegram or ntfy. "
        "Off unless given; nothing is built in.",
    )
    notify_grp.add_argument(
        "--notify-url",
        dest="notify_url",
        default=None,
        help=f"Destination. Falls back to ${NOTIFY_URL_ENV}. Not readable from "
        "the GUI/web config on purpose — this sends data off the machine.",
    )
    notify_grp.add_argument(
        "--notify-token",
        dest="notify_token",
        default=None,
        help=f"Bearer token (ntfy, webhook) or bot token (Telegram). Falls "
        f"back to ${NOTIFY_TOKEN_ENV}.",
    )
    notify_grp.add_argument(
        "--notify-chat-id", dest="notify_chat_id", default="", metavar="ID"
    )
    notify_grp.add_argument(
        "--notify-on",
        dest="notify_on",
        choices=list(NOTIFY_EVENTS),
        default="summary",
        help="'summary' (default) sends one message for the whole run; "
        "'success'/'failure'/'both' send one per track.",
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


# ─────────────────────────────────────────────────────────────
#  Subscriptions (--subscribe / --check-subscriptions)
# ─────────────────────────────────────────────────────────────

SUBSCRIPTION_FLAGS = (
    "--subscribe",
    "--unsubscribe",
    "--subscriptions",
    "--check-subscriptions",
)


def _subscription_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False for the same reason the --web parser sets it:
    # `--subscribe` is a prefix of `--subscribe-groups`/`--subscribe-backfill`,
    # and with abbreviation on argparse calls a bare `--subscribe` ambiguous
    # and exits(2).
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--subscribe", dest="subscribe", default=None, metavar="URL")
    parser.add_argument(
        "--unsubscribe", dest="unsubscribe", default=None, metavar="URL"
    )
    parser.add_argument("--subscriptions", action="store_true")
    parser.add_argument("--check-subscriptions", dest="check", action="store_true")
    parser.add_argument(
        "--subscribe-groups",
        dest="groups",
        default=None,
        metavar="album,single",
        help="Release types to follow: any of "
        f"{', '.join(subscription_groups())} — or 'all'.",
    )
    parser.add_argument(
        "--subscribe-backfill",
        dest="backfill",
        action="store_true",
        help="Treat the existing back catalogue as new. Off by default: a "
        "new subscription records what exists today as already-seen, so the "
        "first thing it fetches is the first thing released after you "
        "subscribed.",
    )
    parser.add_argument(
        "--subscribe-reset",
        dest="reset",
        default=None,
        metavar="URL",
        help="Forget what this subscription has seen, so the whole catalogue "
        "counts as new again.",
    )
    parser.add_argument("--subscribe-name", dest="sub_name", default="", metavar="NAME")
    parser.add_argument("--download", dest="download", action="store_true")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("--profile", dest="profile", default=None)
    parser.add_argument("--output-dir", dest="output_dir", default=None)
    return parser


def subscription_groups() -> tuple[str, ...]:
    from .core.subscriptions import RELEASE_GROUPS

    return RELEASE_GROUPS


def _print_subscriptions(rows: list[dict]) -> None:
    if not rows:
        print("No subscriptions. Add one with: spotiflac --subscribe <artist URL>")
        return
    for row in rows:
        state = "" if row["enabled"] else "  (disabled)"
        checked = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["last_checked_at"]))
            if row["last_checked_at"]
            else "never"
        )
        print(f"{row['name'] or '(unnamed)'}{state}")
        print(f"    {row['url']}")
        print(
            f"    groups: {row['include_groups']}  ·  seen: {row['seen_count']} "
            f"release(s)  ·  last checked: {checked}"
        )
        if row["last_error"]:
            print(f"    last error: {row['last_error']}")
        if row["output_dir"]:
            print(f"    into: {row['output_dir']}")


async def _handle_subscriptions() -> None:
    """Everything behind the --subscribe* / --check-subscriptions flags.

    Kept in one function, and dispatched from amain() the same way --cache-*
    and --trust-key-* are, so a subscription command never falls through into
    the ordinary download path (which would then complain about a missing
    URL).
    """
    from .core import subscriptions

    parser = _subscription_parser()
    args, _ = parser.parse_known_args(sys.argv[1:])

    if args.subscribe:
        try:
            sub = subscriptions.add(
                args.subscribe,
                name=args.sub_name,
                include_groups=args.groups,
                output_dir=args.output_dir or "",
            )
        except subscriptions.SubscriptionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Following {sub.name or sub.url} ({sub.include_groups}).")
        if not args.backfill:
            print(
                "The current catalogue will be recorded as already-seen on the "
                "first check; only later releases are fetched. Use "
                "--subscribe-backfill on the next --check-subscriptions to "
                "fetch what is already out."
            )
        return

    if args.unsubscribe:
        removed = subscriptions.remove_by_url(args.unsubscribe)
        print(
            f"Unfollowed {args.unsubscribe}."
            if removed
            else f"Not following {args.unsubscribe}."
        )
        return

    if args.reset:
        sub = subscriptions.get_by_url(args.reset)
        if sub is None:
            print(f"Not following {args.reset}.", file=sys.stderr)
            sys.exit(1)
        subscriptions.forget_seen(sub.id)
        print(
            f"Reset {sub.name or sub.url}. The next check with "
            "--subscribe-backfill will treat the whole catalogue as new."
        )
        return

    if args.subscriptions:
        rows = [s.to_dict() for s in subscriptions.list_all()]
        (
            print(json.dumps(rows, indent=2))
            if args.as_json
            else _print_subscriptions(rows)
        )
        return

    # --check-subscriptions
    results = await subscriptions.check_all_async(backfill=args.backfill)

    if args.download:
        profile_defaults = (
            await _load_profile_into_defaults(args.profile) if args.profile else {}
        ) or load_config()
        await subscriptions.sync_async(
            results, _subscription_downloader(profile_defaults, args.output_dir)
        )

    if args.as_json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
        return

    total_new = sum(len(r.new) for r in results)
    for result in results:
        label = (
            result.artist_name or result.subscription.name or result.subscription.url
        )
        if result.error:
            print(f"{label}: error — {result.error}")
        elif result.watermarked:
            print(f"{label}: first check, {result.total} release(s) recorded as seen.")
        elif result.new:
            print(f"{label}: {len(result.new)} new release(s)")
            for release in result.new:
                year = f" ({release.year})" if release.year else ""
                print(f"    · {release.title}{year} [{release.type}]")
        else:
            print(f"{label}: nothing new.")
    if total_new and not args.download:
        print(f"\n{total_new} new release(s). Re-run with --download to fetch them.")


def _subscription_downloader(profile_defaults: dict, output_dir_override: str | None):
    """A `download(url, output_dir)` closure over the ordinary download path.

    Subscriptions deliberately own no download settings of their own: a
    fetched release should land with exactly the naming, quality, lyrics and
    tagging the same instance would have used for a manual download, which
    means reading them from the profile/config like every other run does.
    """
    pd = profile_defaults or {}

    async def _download(url: str, sub_output_dir: str) -> None:
        destination = output_dir_override or sub_output_dir or pd.get("output_dir")
        if not destination:
            raise ValueError(
                "No destination for this subscription: pass --output-dir, give "
                "the subscription one when adding it, or save a profile with "
                "an output_dir."
            )
        await _run_download_async(
            url,
            output_dir=destination,
            services=pd.get("services") or ["ext:tidal-web"],
            filename_format=pd.get("filename_format", "{title} - {artist}"),
            use_track_numbers=pd.get("use_track_numbers", False),
            use_album_track_numbers=pd.get("use_album_track_numbers", False),
            use_artist_subfolders=pd.get("use_artist_subfolders", False),
            use_album_subfolders=pd.get("use_album_subfolders", False),
            create_playlist_subfolders=pd.get("create_playlist_subfolders", True),
            loop=None,
            quality=pd.get("quality", "LOSSLESS"),
            first_artist_only=pd.get("first_artist_only", False),
            artist_separator=pd.get("artist_separator"),
            include_featuring=pd.get("include_featuring", False),
            log_level=logging.ERROR,
            output_path=None,
            allow_fallback=pd.get("allow_fallback", True),
            embed_lyrics=pd.get("embed_lyrics", True),
            lyrics_providers=pd.get("lyrics_providers") or ["apple", "lrclib"],
            apple_lyrics_word_by_word=pd.get("apple_lyrics_word_by_word", True),
            save_lrc=pd.get("save_lrc", False),
            lrc_library_dir=pd.get("lrc_library_dir") or None,
            enrich_metadata=pd.get("enrich_metadata", True),
            enrich_providers=pd.get("enrich_providers")
            or ["deezer", "apple", "qobuz", "tidal"],
            qobuz_local_api_url=pd.get("qobuz_local_api_url"),
            tidal_custom_api=pd.get("tidal_custom_api"),
            track_max_retries=pd.get("track_max_retries", 0),
            post_download_action=pd.get("post_download_action", "none"),
            post_download_command=pd.get("post_download_command", ""),
            resume=pd.get("resume", True),
            post_download_hooks=pd.get("post_download_hooks") or [],
            timeout_s=pd.get("timeout_s"),
            transcode_to=pd.get("transcode_to"),
            transcode_bitrate=pd.get("transcode_bitrate", "320k"),
            transcode_keep_original=pd.get("transcode_keep_original", False),
            max_concurrent_downloads=pd.get("max_concurrent_downloads", 2),
            verify_hires=pd.get("verify_hires", False),
        )

    return _download


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


async def _resolve_csv_async(
    csv_path: str,
    *,
    delimiter: str | None,
    min_score: float,
    concurrency: int,
    unresolved_path: str | None,
):
    """Reads and resolves a CSV, reporting on it. Shared by the run and
    `--csv-dry-run`, which are the same work minus the downloading.

    A file that cannot be read, or that holds nothing recognisable, exits
    rather than starting a run that would download zero tracks and call it a
    success — the mistake is almost always a wrong path or a wrong column,
    and both are worth being told about immediately.
    """
    from .core import csv_source
    from .core.errors import SpotiflacError

    logger = logging.getLogger("SpotiFLAC")
    try:
        document = await asyncio.to_thread(
            csv_source.read_rows, csv_path, delimiter=delimiter
        )
    except SpotiflacError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc

    total_rows = len(document.rows)
    logger.info("[csv] %s: %d row(s) to find", document.path, total_rows)

    # A CSV of titles is one catalogue lookup per row, four at a time — a
    # 1875-row file is minutes of a silent terminal. The counter says how
    # far along it is and, separately, how many rows have actually matched,
    # because a run that is finding nothing is worth interrupting early.
    last_line = [0.0]

    def _report(done: int, total: int, found: int) -> None:
        now = time.monotonic()
        if done < total and (now - last_line[0]) < 1.0:
            return
        last_line[0] = now
        logger.info("[csv] matching %d/%d — %d found", done, total, found)

    resolution = await csv_source.resolve_rows(
        document.rows,
        document=document,
        min_score=min_score,
        concurrency=concurrency,
        on_progress=_report,
    )

    logger.info(
        "[csv] %d/%d row(s) matched, %d not found",
        len(resolution.resolved),
        total_rows,
        len(resolution.unresolved),
    )

    if unresolved_path and resolution.unresolved:
        try:
            csv_source.write_unresolved(unresolved_path, resolution.unresolved)
            logger.warning(
                "[csv] %d unmatched row(s) written to %s",
                len(resolution.unresolved),
                unresolved_path,
            )
        except SpotiflacError as exc:
            logger.error("[csv] %s", exc.message)

    return resolution


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
    apple_lyrics_word_by_word: bool = True,
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
    csv_path: str | None = None,
    csv_min_score: float = 0.62,
    csv_concurrency: int = 4,
    csv_delimiter: str | None = None,
    csv_unresolved: str | None = None,
    m3u_format: str = "m3u8",
    max_concurrent_downloads: int = 2,
    verify_hires: bool = False,
    save_lrc: bool = False,
    lrc_library_dir: str | None = None,
    resume: bool = True,
    post_download_hooks: list[str] | None = None,
    json_report: bool = False,
    library_type: str | None = None,
    library_url: str | None = None,
    library_token: str | None = None,
    library_user: str | None = None,
    write_m3u: str | None = None,
    notify: str | None = None,
    notify_url: str | None = None,
    notify_token: str | None = None,
    notify_chat_id: str = "",
    notify_on: str = "summary",
) -> None:
    """Async bridge to SpotiflacDownloader, bypassing the synchronous
    `SpotiFLAC()` wrapper (which would do a nested `asyncio.run()` and
    fallirebbe).
    """
    logger = logging.getLogger("SpotiFLAC")
    # With an output sink installed, something else owns the screen and has
    # already arranged to receive log records (see core/output_sink). Adding
    # a StreamHandler here would do two things wrong at once: write straight
    # to the terminal underneath that UI, and — via `propagate = False` —
    # cut off the handler the UI attached further up the chain.
    if not logger.handlers and not sink_active():
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
    notify_target = None
    if notify:
        from .core.notifiers import NotifierError, build_target, notify_hook

        try:
            notify_target = build_target(
                notify, notify_url, notify_token, notify_chat_id, notify_on
            )
        except NotifierError as exc:
            # Up front, before anything is downloaded: a notifier configured
            # wrongly is a typo to fix now, not something to discover after a
            # two-hour discography finishes and says nothing.
            logger.error("[notify] %s", exc)
            raise SystemExit(2) from exc

    # The report backs --json, --write-m3u, the count in the library-rescan
    # log line, and now the run-summary notification.
    report = (
        RunReport()
        if (json_report or write_m3u or (notify_on == "summary" and notify_target))
        else None
    )
    hooks: list = list(post_download_hooks or [])
    if report is not None:
        hooks.append(report)
    if notify_target is not None and notify_on != "summary":
        hooks.append(notify_hook(notify_target))

    # Every finished track is recorded durably, so quotas have something to
    # count and "have I already got this?" can be answered by ISRC rather
    # than by looking at the filesystem. See core/download_log.py.
    from .core.download_log import record_hook

    hooks.append(record_hook())

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
        apple_lyrics_word_by_word=apple_lyrics_word_by_word,
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
        save_lrc=save_lrc,
        lrc_library_dir=lrc_library_dir,
        resume=resume,
        post_download_hooks=hooks,
    )

    try:
        downloader = SpotiflacDownloader(opts)
        if csv_path:
            # Resolved here rather than inside run_csv_async so the report
            # file (--csv-unresolved) is written even when the download that
            # follows is interrupted: the rows that need fixing are the part
            # of the run the user can act on.
            resolution = await _resolve_csv_async(
                csv_path,
                delimiter=csv_delimiter,
                min_score=csv_min_score,
                concurrency=csv_concurrency,
                unresolved_path=csv_unresolved,
            )
            if loop:
                logger.warning(
                    "--loop is ignored with --csv: run the command again to "
                    "retry what failed.",
                )
            if playlist_urls:
                logger.warning(
                    "--playlist is ignored with --csv: run it separately to "
                    "sync those playlists.",
                )
            await downloader.run_csv_async(
                csv_path,
                resolution=resolution,
                m3u_format=m3u_format,
                min_score=csv_min_score,
                resolve_concurrency=csv_concurrency,
            )
        elif playlist_urls:
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

        if notify_target is not None and notify_on == "summary" and report is not None:
            from .core.notifiers import notify_run_summary

            await notify_run_summary(notify_target, report)

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


async def run_download_from_cfg(cfg: dict, log_level: int) -> None:
    """Runs one guided-mode configuration, `--loop`/`--watch` included.

    The single place a `cfg` dict becomes a download. Both guided frontends
    call it — the wizard and the TUI — because the alternative is two copies
    of a forty-argument call, and the second copy is the one that stops
    getting new flags. `tests/test_tui_config_state.py` reads the keys out of
    this function to check the TUI still produces all of them.
    """

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
            apple_lyrics_word_by_word=cfg.get("apple_lyrics_word_by_word", True),
            save_lrc=cfg.get("save_lrc", False),
            lrc_library_dir=cfg.get("lrc_library_dir") or None,
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
            # The wizard takes a .csv where it takes a link (see
            # interactive.py's URL step); everything after that point is
            # the same run.
            csv_path=cfg.get("csv_path") or None,
        )

    await _run_once()

    if cfg.get("watch"):
        await watch_forever(_run_once, cfg["watch"])


async def amain() -> None:
    """Coordinate GUI, interactive, and command-line execution for SpotiFLAC.

    Handles startup checks, extension installation, configuration loading, profile management, argument parsing, and download execution across the supported application modes.
    """
    from .core.ffmpeg_check import print_ffmpeg_warning
    from .core.node_check import print_node_warning

    _print_welcome_banner()

    # Nothing below this point is needed to print usage, and all of it
    # touches the network. Skipping it for --help is what turns the first
    # command anyone runs from a multi-second wait — or an apparent hang,
    # when a configured registry is unreachable — back into instant output.
    help_only = _is_help_invocation()

    if not help_only:
        with contextlib.suppress(Exception):
            await check_for_updates_async()

    _register_cli_registries(_early_registries_from_argv())
    _register_cli_registry_directories(_early_registry_directories_from_argv())

    if not help_only:
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

    if _argv_has("--tui", "--interactive"):
        if "--interactive" in sys.argv and "--tui" not in sys.argv:
            # A warning rather than a silent redirect: the two are not the
            # same shape, and somebody driving the wizard from a script
            # deserves to be told before the screen changes under them.
            warnings.warn(
                "--interactive is deprecated and now opens --tui, the "
                "terminal UI. It will be removed in a future release; use "
                "--tui, or the command line for a scripted run.",
                DeprecationWarning,
                stacklevel=2,
            )
            print(
                "--interactive now opens the terminal UI (--tui). "
                "For a scripted or piped run, use the command line instead: "
                "spotiflac --help",
                file=sys.stderr,
            )

        print_ffmpeg_warning()
        print_node_warning()

        from .tui.app import run_tui_async

        await run_tui_async(min_trust_tier=_early_min_trust_from_argv())
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
        user_parser.add_argument(
            "--role",
            choices=list(WEB_USER_ROLES),
            default="user",
            help="'admin' may see instance-wide metrics and manage other "
            "accounts' quotas; 'user' (default) may not.",
        )
        user_parser.add_argument(
            "--daily-tracks",
            type=int,
            default=0,
            help="Tracks this account may download per rolling 24h. "
            "0 (default) is unlimited.",
        )
        user_parser.add_argument(
            "--daily-mb",
            type=int,
            default=0,
            help="Megabytes per rolling 24h. 0 (default) is unlimited.",
        )
        user_args, _ = user_parser.parse_known_args(sys.argv[1:])

        from .core.web_users import WebUserError, create_user

        try:
            create_user(
                *user_args.creds,
                role=user_args.role,
                daily_track_quota=user_args.daily_tracks,
                daily_byte_quota=user_args.daily_mb * 1024 * 1024,
            )
        except WebUserError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Web user '{user_args.creds[0]}' created ({user_args.role}).")
        return

    if _argv_has("--web-user-quota"):
        quota_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        quota_parser.add_argument(
            "--web-user-quota", dest="username", metavar="USERNAME"
        )
        quota_parser.add_argument("--daily-tracks", type=int, default=None)
        quota_parser.add_argument("--daily-mb", type=int, default=None)
        quota_args, _ = quota_parser.parse_known_args(sys.argv[1:])

        from .core.web_users import quota_usage, set_quota

        updated = set_quota(
            quota_args.username,
            daily_track_quota=quota_args.daily_tracks,
            daily_byte_quota=(
                None
                if quota_args.daily_mb is None
                else quota_args.daily_mb * 1024 * 1024
            ),
        )
        if not updated:
            print(f"No web user named '{quota_args.username}'.", file=sys.stderr)
            sys.exit(1)
        usage = quota_usage(quota_args.username)
        print(
            f"{quota_args.username}: "
            f"{usage['tracks_used']}/{usage['tracks_limit'] or '∞'} tracks, "
            f"{usage['bytes_used'] // (1024 * 1024)}/"
            f"{(usage['bytes_limit'] // (1024 * 1024)) if usage['bytes_limit'] else '∞'}"
            f" MB in the last {int(usage['window_hours'])}h."
        )
        return

    if "--web-user-role" in sys.argv:
        role_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        role_parser.add_argument(
            "--web-user-role",
            dest="pair",
            nargs=2,
            metavar=("USERNAME", "ROLE"),
        )
        role_args, _ = role_parser.parse_known_args(sys.argv[1:])

        from .core.web_users import WebUserError, set_role

        try:
            updated = set_role(*role_args.pair)
        except WebUserError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(
            f"'{role_args.pair[0]}' is now {role_args.pair[1]}."
            if updated
            else f"No web user named '{role_args.pair[0]}'."
        )
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

    if _argv_has("--stats"):
        from .core import stats

        # allow_abbrev=False: "--stats" is a prefix of every other flag in
        # this group, and argparse would rather exit(2) than guess.
        stats_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        stats_parser.add_argument("--stats", action="store_true")
        stats_parser.add_argument(
            "--stats-year", dest="year", type=int, default=None, metavar="YYYY"
        )
        stats_parser.add_argument(
            "--stats-days", dest="days", type=int, default=None, metavar="N"
        )
        stats_parser.add_argument(
            "--stats-user", dest="owner", default=None, metavar="USERNAME"
        )
        stats_parser.add_argument(
            "--stats-top", dest="top", type=int, default=stats.DEFAULT_TOP
        )
        stats_parser.add_argument("--json", dest="as_json", action="store_true")
        stats_args, _ = stats_parser.parse_known_args(sys.argv[1:])

        window = stats.parse_window(year=stats_args.year, days=stats_args.days)
        document = await asyncio.to_thread(
            stats.wrapped,
            owner=stats_args.owner,
            window=window,
            top=max(1, stats_args.top),
        )
        print(
            json.dumps(document, indent=2)
            if stats_args.as_json
            else stats.format_wrapped(document)
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
        from .core.web_users import list_users

        users = list_users()
        if not users:
            print("No web users configured.")
            return
        for user in users:
            tracks = user["daily_track_quota"] or "∞"
            size = (
                f"{user['daily_byte_quota'] // (1024 * 1024)} MB"
                if user["daily_byte_quota"]
                else "∞"
            )
            print(
                f"{user['username']}  [{user['role']}]  {tracks} tracks/day, {size}/day"
            )
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
        # Names only, by construction: trusted_signer_names() never returns
        # key material, so this listing cannot print any. Read
        # trusted_keys.json directly if you need the public key bytes.
        from .extensions.trust import trusted_signer_names

        names = trusted_signer_names()
        if not names:
            print("No trusted keys configured.")
        for idx, name in enumerate(names, start=1):
            print(f"key-{idx}: {name or '(unnamed)'}")
        return

    if _argv_has("--dedup-restore"):
        restore_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        restore_parser.add_argument("--dedup-restore", dest="manifest", required=True)
        restore_parser.add_argument("--json", dest="as_json", action="store_true")
        restore_parser.add_argument("--verbose", "-v", action="store_true")
        restore_args, _ = restore_parser.parse_known_args(sys.argv[1:])

        from .tools.library_dedup_cli import run_restore

        run_restore(
            restore_args.manifest,
            as_json=restore_args.as_json,
            verbose=restore_args.verbose,
        )
        return

    if _argv_has("--dedup-library", "--dedup-from-db"):
        # allow_abbrev=False for the same reason the upgrade block below
        # needs it: `--dedup-library` is a prefix of `--dedup-library-…`
        # were one ever added, and an ambiguous flag exits(2) instead of
        # running.
        dd_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        # Not required: --dedup-from-db carries the library's identity in
        # the database itself, so a resolution run needs no folder.
        dd_parser.add_argument("--dedup-library", dest="path", default="")
        dd_parser.add_argument(
            "--dedup-match",
            dest="match",
            choices=("isrc", "tags", "both"),
            default="both",
            help="Which signal decides that two files are one recording "
            "(default: both — ISRC first, artist/title+duration for the rest).",
        )
        dd_parser.add_argument(
            "--dedup-tolerance", dest="tolerance", type=float, default=4.0
        )
        dd_parser.add_argument(
            "--dedup-keep-version-noise",
            dest="keep_version_noise",
            action="store_true",
            help="Treat '(2011 Remaster)' as part of the title, so a "
            "remaster is not a duplicate of the original.",
        )
        dd_parser.add_argument(
            "--dedup-verify",
            dest="verify",
            action="store_true",
            help="Confirm each group against the audio with Chromaprint "
            "before offering it. Needs the 'dedup' extra.",
        )
        dd_parser.add_argument(
            "--dedup-threshold", dest="threshold", type=float, default=0.95
        )
        dd_parser.add_argument(
            "--dedup-no-cache", dest="use_cache", action="store_false"
        )
        dd_parser.add_argument(
            "--dedup-apply",
            dest="apply",
            action="store_true",
            help="Actually resolve them. Without it the command only reports "
            "(a scan is safe; removing files is not).",
        )
        dd_parser.add_argument(
            "--dedup-delete",
            dest="delete",
            action="store_true",
            help="With --dedup-apply, unlink instead of quarantining. Not undoable.",
        )
        dd_parser.add_argument("--dedup-trash", dest="trash_dir", default=None)
        dd_parser.add_argument("--dedup-limit", dest="limit", type=int, default=None)
        dd_parser.add_argument(
            "--dedup-db",
            dest="db_path",
            default=None,
            metavar="FILE.db",
            help="Also write the scan to a SQLite database: one row per "
            "file, duplicate groups on top of it.",
        )
        dd_parser.add_argument(
            "--dedup-from-db",
            dest="from_db",
            default=None,
            metavar="FILE.db",
            help="Skip the scan and read the report back from a database a "
            "previous run wrote.",
        )
        dd_parser.add_argument("--no-recursive", dest="recursive", action="store_false")
        dd_parser.add_argument("--json", dest="as_json", action="store_true")
        dd_parser.add_argument("--verbose", "-v", action="store_true")
        dd_args, _ = dd_parser.parse_known_args(sys.argv[1:])

        from .tools.library_dedup_cli import run as run_dedup

        if not dd_args.path and not dd_args.from_db:
            print(
                "Error: --dedup-library needs a folder, or --dedup-from-db a "
                "database a previous run wrote.",
                file=sys.stderr,
            )
            sys.exit(1)

        run_dedup(
            dd_args.path,
            recursive=dd_args.recursive,
            match=dd_args.match,
            duration_tolerance_s=dd_args.tolerance,
            keep_version_noise=dd_args.keep_version_noise,
            verify=dd_args.verify,
            threshold=dd_args.threshold,
            use_cache=dd_args.use_cache,
            apply=dd_args.apply,
            delete=dd_args.delete,
            trash_dir=dd_args.trash_dir,
            limit=dd_args.limit,
            db_path=dd_args.db_path,
            from_db=dd_args.from_db,
            as_json=dd_args.as_json,
            verbose=dd_args.verbose,
        )
        return

    if _argv_has("--upgrade-library"):
        # allow_abbrev=False: `--upgrade-library` is a prefix of
        # `--upgrade-library-target`, and argparse would call a bare
        # `--upgrade-library` ambiguous and exit(2).
        up_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        up_parser.add_argument("--upgrade-library", dest="path", required=True)
        up_parser.add_argument(
            "--upgrade-target",
            dest="target",
            default="LOSSLESS",
            help="Quality to bring the library up to (default: LOSSLESS). "
            "Anything normalize_quality() understands: LOSSLESS, HI_RES, …",
        )
        up_parser.add_argument(
            "--upgrade-verify-hires",
            dest="verify",
            action="store_true",
            help="Also reclassify files that declare Hi-Res but whose audio "
            "stops at CD range. Needs the 'hires' extra, and decodes ~30s per "
            "file — slow on a large library.",
        )
        up_parser.add_argument(
            "--upgrade-download",
            dest="download",
            action="store_true",
            help="Actually re-download what was found. Without this the "
            "command only reports (a scan is safe; a re-download is not).",
        )
        up_parser.add_argument("--upgrade-limit", dest="limit", type=int, default=None)
        up_parser.add_argument("--no-recursive", dest="recursive", action="store_false")
        up_parser.add_argument("--verbose", "-v", action="store_true")
        up_parser.add_argument("--json", dest="as_json", action="store_true")
        up_parser.add_argument("--profile", dest="profile", default=None)
        up_parser.add_argument("--output-dir", dest="output_dir", default=None)
        up_args, _ = up_parser.parse_known_args(sys.argv[1:])

        from .tools.library_upgrade_cli import run as run_upgrade_scan
        from .tools.library_upgrade_cli import run_and_upgrade_async

        if not up_args.download:
            run_upgrade_scan(
                up_args.path,
                up_args.target,
                recursive=up_args.recursive,
                verify_hires=up_args.verify,
                as_json=up_args.as_json,
                verbose=up_args.verbose,
            )
            return

        profile_defaults = (
            await _load_profile_into_defaults(up_args.profile)
            if up_args.profile
            else {}
        ) or load_config()
        # The upgraded file goes back where the library already is unless
        # told otherwise, which is what makes this an *upgrade* rather than a
        # second copy somewhere else.
        destination = (
            up_args.output_dir or profile_defaults.get("output_dir") or up_args.path
        )
        downloader = _subscription_downloader(
            {**profile_defaults, "quality": up_args.target}, destination
        )

        async def _download(url: str) -> None:
            await downloader(url, destination)

        await run_and_upgrade_async(
            up_args.path,
            up_args.target,
            _download,
            recursive=up_args.recursive,
            verify_hires=up_args.verify,
            limit=up_args.limit,
            as_json=up_args.as_json,
            verbose=up_args.verbose,
        )
        return

    if _argv_has(*SUBSCRIPTION_FLAGS, "--subscribe-reset"):
        await _handle_subscriptions()
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

    if args.csv_path:
        # `spotiflac --csv tracks.csv DEST` gives argparse a single
        # positional, which it binds to `url`. The file is the input here, so
        # that positional is the destination.
        if not output_dir and args.url:
            output_dir, args.url = args.url, ""
        elif args.url:
            logger_ = logging.getLogger("SpotiFLAC")
            logger_.warning(
                "[csv] the URL argument is ignored with --csv; run it "
                "separately to download it.",
            )
            args.url = ""

    if args.csv_path and args.csv_dry_run:
        # No destination needed and nothing downloaded: this is the "what
        # would this file do?" pass, and it must run before the check below
        # that a destination was given at all.
        from .core import csv_source

        resolution = await _resolve_csv_async(
            args.csv_path,
            delimiter=args.csv_delimiter,
            min_score=(
                args.csv_min_score
                if args.csv_min_score is not None
                else csv_source.DEFAULT_MIN_SCORE
            ),
            concurrency=args.csv_concurrency,
            unresolved_path=args.csv_unresolved,
        )
        if args.json_report:
            print(json.dumps(resolution.to_dict(), indent=2))
        else:
            print(csv_source.format_resolution(resolution, verbose=True))
        return

    if not (args.url or playlist_urls or args.csv_path) or not output_dir:
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

    log_level = _resolve_log_level(args.verbose, args.log_level, args.profile_log_level)
    log_format = (
        "%(levelname)s:%(name)s: %(message)s"
        if args.verbose
        else "%(levelname)s: %(message)s"
    )
    _cli_handler = logging.StreamHandler(sys.stdout)
    _cli_handler.setFormatter(_CleanConsoleFormatter(log_format))
    logging.basicConfig(level=log_level, handlers=[_cli_handler])
    _quiet_noisy_libraries(log_level)

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
            allow_fallback=args.allow_fallback,
            embed_lyrics=args.embed_lyrics,
            lyrics_providers=args.lyrics_providers,
            apple_lyrics_word_by_word=args.apple_lyrics_word_by_word,
            save_lrc=args.save_lrc,
            lrc_library_dir=args.lrc_library_dir,
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
            csv_path=args.csv_path,
            csv_min_score=(
                args.csv_min_score if args.csv_min_score is not None else 0.62
            ),
            csv_concurrency=args.csv_concurrency,
            csv_delimiter=args.csv_delimiter,
            csv_unresolved=args.csv_unresolved,
            m3u_format=args.m3u_format,
            max_concurrent_downloads=args.max_concurrent,
            verify_hires=args.verify_hires,
            notify=args.notify,
            notify_url=args.notify_url,
            notify_token=args.notify_token,
            notify_chat_id=args.notify_chat_id,
            notify_on=args.notify_on,
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
                "allow_fallback": args.allow_fallback,
                "embed_lyrics": args.embed_lyrics,
                "lyrics_providers": args.lyrics_providers,
                "apple_lyrics_word_by_word": args.apple_lyrics_word_by_word,
                "enrich_metadata": args.enrich,
                "enrich_providers": args.enrich_providers,
                "log_level": log_level,
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
                "save_lrc": args.save_lrc,
                "lrc_library_dir": args.lrc_library_dir,
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

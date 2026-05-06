"""
SpotiFLAC — Interactive Mode.
Guides the user through the step-by-step configuration without needing to remember CLI flags.
"""
from __future__ import annotations
import os
import sys

# ---------------------------------------------------------------------------
# ANSI colors (works on macOS, Linux, Windows 10+)
# ---------------------------------------------------------------------------
_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

BOLD   = lambda t: _c("1", t)
DIM    = lambda t: _c("2", t)
CYAN   = lambda t: _c("96", t)
GREEN  = lambda t: _c("92", t)
YELLOW = lambda t: _c("93", t)
RED    = lambda t: _c("91", t)
BLUE   = lambda t: _c("94", t)
MAGENTA= lambda t: _c("95", t)


# ---------------------------------------------------------------------------
# Primitive input helpers
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    """Asks the user for a string."""
    default_hint = f" {DIM('[' + default + ']')}" if default else ""
    try:
        val = input(f"  {prompt}{default_hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def _ask_bool(prompt: str, default: bool = False) -> bool:
    """Asks for a yes/no confirmation."""
    hint = DIM("Y/n" if default else "y/N")
    try:
        val = input(f"  {prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not val:
        return default
    return val in ("y", "yes", "s", "si", "1")


def _ask_choice(prompt: str, options: list[str], default: str) -> str:
    """Asks the user to choose an option from a list."""
    print(f"\n  {BOLD(prompt)}")
    for i, opt in enumerate(options, 1):
        marker = GREEN("▶") if opt == default else " "
        print(f"    {marker} {DIM(f'[{i}]')} {opt}")
    print(f"    {DIM('Enter = default')}")
    try:
        val = input("  → ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not val:
        return default
    if val.isdigit() and 1 <= int(val) <= len(options):
        return options[int(val) - 1]
    if val in options:
        return val
    return default


def _ask_multi(
        prompt: str,
        options: list[str],
        defaults: list[str],
        ordered: bool = False,
) -> list[str]:
    """
    Asks the user to select multiple options.
    If ordered=True, the insertion order is preserved.
    """
    print(f"\n  {BOLD(prompt)}")
    for i, opt in enumerate(options, 1):
        marker = GREEN("●") if opt in defaults else DIM("○")
        default_label = DIM(" (default)") if opt in defaults else ""
        print(f"    {DIM(f'[{i}]')} {marker} {opt}{default_label}")
    print(f"    {DIM('Enter numbers separated by space (e.g., 1 3 2) — Enter = default')}")
    try:
        val = input("  → ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if not val:
        return list(defaults)

    tokens = val.split()
    if ordered:
        result = []
        seen = set()
        for t in tokens:
            if t.isdigit() and 1 <= int(t) <= len(options):
                opt = options[int(t) - 1]
                if opt not in seen:
                    result.append(opt)
                    seen.add(opt)
        return result if result else list(defaults)
    else:
        result = [options[int(t) - 1] for t in tokens
                  if t.isdigit() and 1 <= int(t) <= len(options)]
        return result if result else list(defaults)


# ---------------------------------------------------------------------------
# Wizard Sections
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    width = 50
    print(f"\n{CYAN('─' * width)}")
    print(f"{BOLD(CYAN(f'  {title}'))}")
    print(f"{CYAN('─' * width)}")


def _header() -> None:
    print()
    print(CYAN(BOLD("  ╔══════════════════════════════════════════════╗")))
    print(CYAN(BOLD("  ║        SpotiFLAC  —  Download Wizard         ║")))
    print(CYAN(BOLD("  ╚══════════════════════════════════════════════╝")))
    print(f"  {DIM('Press Ctrl+C at any time to exit')}")


def _summary(cfg: dict) -> None:
    _section("Configuration Summary")

    def row(label: str, value: str) -> None:
        print(f"  {BOLD(label + ':'): <30} {GREEN(value)}")

    row("URL", cfg["url"])
    row("Output Dir", cfg["output_dir"])

    # Add this check for the custom path
    if cfg.get("output_path"):
        row("Exact File Path", cfg["output_path"])
    row("Services", " → ".join(cfg["services"]))
    row("Quality", cfg["quality"])
    row("Filename format", cfg["filename_format"])

    flags = []
    if cfg["use_track_numbers"]:     flags.append("track-numbers")
    if cfg["use_album_track_numbers"]: flags.append("album-track-numbers")
    if cfg["use_artist_subfolders"]: flags.append("artist-subfolders")
    if cfg["use_album_subfolders"]:  flags.append("album-subfolders")
    if cfg["first_artist_only"]:     flags.append("first-artist-only")
    if cfg["include_featuring"]:     flags.append("include-featuring")
    row("Options", ", ".join(flags) if flags else "none")

    row("Lyrics", "enabled (" + ", ".join(cfg["lyrics_providers"]) + ")" if cfg["embed_lyrics"] else "disabled")
    row("Enrichment", "enabled (" + ", ".join(cfg["enrich_providers"]) + ")" if cfg["enrich_metadata"] else "disabled")

    if cfg.get("qobuz_token"):
        row("Qobuz token", "✓ set")
    if cfg.get("lyrics_spotify_token"):
        row("Spotify token", "✓ set")
    if cfg.get("loop"):
        row("Loop", f"every {cfg['loop']} minutes")


# ---------------------------------------------------------------------------
# Main Wizard
# ---------------------------------------------------------------------------

def run_interactive() -> dict:
    """
    Runs the interactive wizard and returns a configuration dict
    compatible with SpotiFLAC() parameters.
    """
    _header()

    cfg: dict = {}

    # ── 1. URL ──────────────────────────────────────────────────────────────
    _section("1 · URL")
    print(f"  {DIM('Supported: Spotify and Tidal (track, album, playlist, artist)')}")
    url = ""
    while not url:
        url = _ask("URL")
        if not url:
            print(f"  {RED('⚠  URL is required.')}")
    cfg["url"] = url

    # ── 2. Output directory ─────────────────────────────────────────────────
    _section("2 · Output Directory")
    cfg["output_dir"] = _ask("Destination folder", "./Downloads")

    # ── 2.5. Custom Output Path (Only for single tracks) ────────────────────
    if "/track/" in cfg["url"]:
        _section("2.5 · Custom Output Path")
        print(f"  {DIM('Since this is a single track, you can specify an exact filename.')}")
        print(f"  {DIM('Example: my_files/favorite_song.flac')}")

        use_custom = _ask_bool("Do you want to set a custom output path?", False)
        if use_custom:
            cfg["output_path"] = _ask("Full file path including .flac " + DIM("(e.g., /Users/Name/Desktop/song.flac)"))
        else:
            cfg["output_path"] = None
    else:
        cfg["output_path"] = None

    # ── 3. Services ──────────────────────────────────────────────────────────
    _section("3 · Audio Services")
    print(f"  {DIM('Choose the services and their priority order (the first has priority)')}")
    services = _ask_multi(
        "Services (order = priority):",
        options  = ["tidal", "qobuz", "amazon", "spoti"],
        defaults = ["tidal"],
        ordered  = True,
    )
    cfg["services"] = services

    # ── 4. Quality ──────────────────────────────────────────────────────────
    _section("4 · Audio Quality")
    print(f"  {DIM('Note: If the requested quality is not found, an automatic fallback will be executed.')}")

    has_qobuz = "qobuz" in cfg["services"]
    has_tidal = "tidal" in cfg["services"]

    if has_qobuz and not has_tidal:
        # The user chose Qobuz but not Tidal
        q_choice = _ask_choice(
            "Qobuz Quality:",
            options = ["6 (CD Lossless)", "7 (Hi-Res)", "27 (Hi-Res Max)"],
            default = "6 (CD Lossless)",
        )
        # Extract only the number (6, 7 or 27) from the chosen string
        cfg["quality"] = q_choice.split(" ")[0]

    elif has_tidal and not has_qobuz:
        # The user chose Tidal but not Qobuz
        cfg["quality"] = _ask_choice(
            "Tidal Quality:",
            options = ["LOSSLESS", "HI_RES"],
            default = "LOSSLESS",
        )

    elif has_qobuz and has_tidal:
        # The user included both providers in the list
        print(f"  {DIM('You selected both Qobuz and Tidal. Choose a unified profile:')}")
        q_choice = _ask_choice(
            "Combined Quality:",
            options = [
                "LOSSLESS (Applies '6' on Qobuz and 'LOSSLESS' on Tidal)",
                "HI_RES (Applies '27' on Qobuz and 'HI_RES' on Tidal)",
                "7 (Applies intermediate Hi-Res quality only for Qobuz)"
            ],
            default = "LOSSLESS (Applies '6' on Qobuz and 'LOSSLESS' on Tidal)",
        )
        # Reduce the long string to the key value required by the CLI
        if q_choice.startswith("LOSSLESS"):
            cfg["quality"] = "LOSSLESS"
        elif q_choice.startswith("HI_RES"):
            cfg["quality"] = "HI_RES"
        else:
            cfg["quality"] = "7"

    else:
        # Fallback in case only Amazon or Spotify are used
        cfg["quality"] = _ask_choice(
            "Quality:",
            options = ["LOSSLESS", "HI_RES"],
            default = "LOSSLESS",
        )

    # ── 5. Filename format ─────────────────────────────────────────────────
    _section("5 · Filename Format")
    print(f"  {DIM('Placeholders: {title} {artist} {album} {album_artist} {year} {date} {track} {disc} {isrc} {position}')}")
    cfg["filename_format"] = _ask("Format", "{title} - {artist}")

    # ── 6. Organization options ───────────────────────────────────────────
    _section("6 · Organization Options")

    # Ask first if they want track numbers
    cfg["use_track_numbers"] = _ask_bool("Add track number to filename?", False)

    # If yes, ask which type of numbering to use
    if cfg["use_track_numbers"]:
        cfg["use_album_track_numbers"] = _ask_bool("Use original album track number?", False)
    else:
        # If no, automatically set the variable to False without asking
        cfg["use_album_track_numbers"] = False

    cfg["use_artist_subfolders"]   = _ask_bool("Create artist subfolders?", False)
    cfg["use_album_subfolders"]    = _ask_bool("Create album subfolders?", False)
    cfg["first_artist_only"]       = _ask_bool("Use only the first artist in tags and filename?", False)

    # ── 7. Featuring ────────────────────────────────────────────────────────
    _section("7 · Featuring")

    if "/artist/" in cfg["url"]:
        print("  " + DIM("If enabled, also downloads individual tracks where the artist appears as a featured artist"))
        print("  " + DIM("on other artists' releases (appears_on on Spotify, compilations on Tidal)"))
        cfg["include_featuring"] = _ask_bool("Include featuring tracks?", False)
    else:
        # Show the user that we are skipping this section
        print(f"  {YELLOW('⏭  Skipped:')} {DIM('The provided URL does not belong to an artist page.')}")
        cfg["include_featuring"] = False

    # ── 8. Lyrics ────────────────────────────────────────────────────────────
    _section("8 · Lyrics")
    cfg["embed_lyrics"] = _ask_bool("Embed synchronized lyrics?", True)

    if cfg["embed_lyrics"]:
        cfg["lyrics_providers"] = _ask_multi(
            "Lyrics providers (order = priority):",
            options  = ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
            defaults = ["spotify", "musixmatch", "lrclib", "apple"],
            ordered  = True,
        )
        has_spotify = "spotify" in cfg["lyrics_providers"]
        if has_spotify:
            print(f"  {DIM('The Spotify provider requires the sp_dc cookie for synchronized lyrics')}")
            token = _ask("Spotify sp_dc cookie (leave blank to skip)", "")
            cfg["lyrics_spotify_token"] = token
        else:
            cfg["lyrics_spotify_token"] = ""
    else:
        cfg["lyrics_providers"]      = ["spotify", "musixmatch", "lrclib", "apple"]
        cfg["lyrics_spotify_token"]  = ""

    # ── 9. Metadata enrichment ──────────────────────────────────────────────
    _section("9 · Metadata Enrichment")
    print(f"  {DIM('Adds genre, BPM, label, HD cover, MusicBrainz IDs, and more')}")
    cfg["enrich_metadata"] = _ask_bool("Enable metadata enrichment?", True)

    if cfg["enrich_metadata"]:
        cfg["enrich_providers"] = _ask_multi(
            "Enrichment providers (order = priority):",
            options  = ["deezer", "apple", "qobuz", "tidal"],
            defaults = ["deezer", "apple", "qobuz", "tidal"],
            ordered  = True,
        )
    else:
        cfg["enrich_providers"] = ["deezer", "apple", "qobuz", "tidal"]

    # ── 10. Optional Tokens ─────────────────────────────────────────────────────
    _section("10 · Optional Tokens")
    cfg["qobuz_token"] = _ask("Qobuz auth token (leave blank to skip)", "") or None

    # ── 11. Loop ────────────────────────────────────────────────────────────
    loop_str = _ask("Repeat every N minutes (leave blank to disable)", "")
    cfg["loop"] = int(loop_str) if loop_str.isdigit() else None

    # ── Summary + confirmation ─────────────────────────────────────────────────
    _summary(cfg)
    print()
    if not _ask_bool(BOLD("Start download with this configuration?"), True):
        print(f"\n  {YELLOW('Operation cancelled.')}\n")
        sys.exit(0)

    # ── Equivalent CLI command ──────────────────────────────────────────────
    _section("Equivalent CLI command")
    _print_cli_command(cfg)

    return cfg


def _print_cli_command(cfg: dict) -> None:
    """Prints the CLI command equivalent to the chosen configuration."""
    parts = [f'spotiflac "{cfg["url"]}" "{cfg["output_dir"]}"']
    if cfg.get("output_path"):
        parts.append(f'-o "{cfg["output_path"]}"')
    parts.append(f'-s {" ".join(cfg["services"])}')
    if cfg["quality"] != "LOSSLESS":
        parts.append(f'-q {cfg["quality"]}')
    if cfg["filename_format"] != "{title} - {artist}":
        parts.append(f'--filename-format "{cfg["filename_format"]}"')
    if cfg["use_track_numbers"]:        parts.append("--use-track-numbers")
    if cfg["use_album_track_numbers"]:  parts.append("--use-album-track-numbers")
    if cfg["use_artist_subfolders"]:    parts.append("--use-artist-subfolders")
    if cfg["use_album_subfolders"]:     parts.append("--use-album-subfolders")
    if cfg["first_artist_only"]:        parts.append("--first-artist-only")
    if cfg["include_featuring"]:        parts.append("--include-featuring")
    if not cfg["embed_lyrics"]:
        parts.append("--no-lyrics")
    else:
        parts.append(f'--lyrics-providers {" ".join(cfg["lyrics_providers"])}')
        if cfg.get("lyrics_spotify_token"):
            parts.append(f'--spotify-token "{cfg["lyrics_spotify_token"]}"')
    if not cfg["enrich_metadata"]:
        parts.append("--no-enrich")
    else:
        parts.append(f'--enrich-providers {" ".join(cfg["enrich_providers"])}')
    if cfg.get("qobuz_token"):
        parts.append(f'--qobuz-token "{cfg["qobuz_token"]}"')
    if cfg.get("loop"):
        parts.append(f'--loop {cfg["loop"]}')

    cmd = " \\\n    ".join(parts)
    print(f"\n  {DIM(cmd)}\n")
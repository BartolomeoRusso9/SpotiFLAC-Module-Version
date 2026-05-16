"""
SpotiFLAC — Interactive Mode.
New features compared to previous version:
  - Automatic health check at startup
  - URL history with quick selection
  - Last output folder as default
  - Profile management (load / save)
  - Per-track retry section
  - Post-download actions section
"""
from __future__ import annotations
from urllib.parse import urlparse
import os
import sys

_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

BOLD    = lambda t: _c("1", t)
DIM     = lambda t: _c("2", t)
CYAN    = lambda t: _c("96", t)
GREEN   = lambda t: _c("92", t)
YELLOW  = lambda t: _c("93", t)
RED     = lambda t: _c("91", t)
BLUE    = lambda t: _c("94", t)
MAGENTA = lambda t: _c("95", t)


def _ask(prompt: str, default: str = "") -> str:
    default_hint = f" {DIM('[' + default + ']')}" if default else ""
    try:
        val = input(f"  {prompt}{default_hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def _ask_bool(prompt: str, default: bool = False) -> bool:
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


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

_ALL_SERVICES = ["tidal", "qobuz", "deezer", "amazon", "soundcloud", "apple", "youtube", "pandora", "spoti"]

def _run_health_check():
    try:
        from SpotiFLAC.core.health_check import run_health_check
        return run_health_check(_ALL_SERVICES, include_all_endpoints=True)
    except Exception as e:
        print(f"  {RED(f'Health check error: {e}')}")
        return []


def _display_health_check() -> dict[str, bool]:
    """Mostra il risultato del health check in formato semplificato senza URL."""
    _section("Service Availability Check")
    print(f"  {DIM('Probing endpoints...')} ", end="", flush=True)

    results = _run_health_check()

    # Cancella la riga "Probing…"
    print("\r" + " " * 40 + "\r", end="")

    if not results:
        print(f"  {YELLOW('⚠  Health check skipped (import error or no network)')}")
        return {}

    # Organizza i risultati: ci basta sapere se il provider ha almeno un endpoint OK
    status = {svc: False for svc in _ALL_SERVICES}
    for r in results:
        if r.ok:
            status[r.provider] = True

    # Stampa i provider in verticale con lo stato testuale
    for svc in _ALL_SERVICES:
        ok = status[svc]
        icon = GREEN("✅") if ok else RED("❌")
        print(f"  {icon} {BOLD(svc)}")

        if ok:
            print(f"      {DIM('↳')} {GREEN('reachable')}")
        else:
            print(f"      {DIM('↳')} {RED('no reachable endpoints')}")

    working_count = sum(status.values())
    total = len(_ALL_SERVICES)
    summary_color = GREEN if working_count == total else (YELLOW if working_count > 0 else RED)
    print(f"\n  {summary_color(f'{working_count}/{total} providers reachable')}")

    if working_count == 0:
        print(f"\n  {RED('✗  No providers reachable — check your internet connection.')}")

    return status


# ---------------------------------------------------------------------------
# URL History
# ---------------------------------------------------------------------------

def _pick_from_history() -> str | None:
    try:
        from SpotiFLAC.core.session_memory import get_url_history, remove_url_from_history, clear_url_history
    except Exception:
        return None

    while True:
        history = get_url_history()

        if not history:
            return None

        _section("Recent URLs  (optional)")
        print(f"  {DIM('Press Enter to type a new URL, or choose a recent one:')}")
        print()

        for i, entry in enumerate(history[:8], 1):
            label = entry.get("label", entry.get("url", ""))[:55]
            url_short = entry.get("url", "")[:60]
            print(f"    {DIM(f'[{i}]')} {label}")
            if label != url_short:
                print(f"         {DIM(url_short)}")

        print(f"\n    {DIM('[Enter]')} Type a new URL to create a queue")
        print(f"    {DIM('[d + num]')} Delete an entry (e.g., d2, d5)")
        print(f"    {DIM('[c]')} Clear all history")

        try:
            val = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if not val:
            return None

        val_lower = val.lower()

        if val_lower in ('c', 'clear'):
            if _ask_bool("Are you sure you want to clear ALL history?", False):
                try:
                    clear_url_history()
                    print(f"\n  {GREEN('✓')} History cleared.\n")
                    continue
                except Exception as e:
                    print(f"\n  {RED('✗')} Could not clear history: {e}\n")
                    continue
            else:
                print()
                continue

        if val_lower.startswith('d') and len(val_lower) > 1:
            num_str = val_lower[1:].strip()
            if num_str.isdigit():
                idx = int(num_str) - 1
                if 0 <= idx < len(history[:8]):
                    url_to_remove = history[idx].get("url")
                    try:
                        remove_url_from_history(url_to_remove)
                        print(f"\n  {GREEN('✓')} Entry deleted. Refreshing list...\n")
                        continue
                    except Exception as e:
                        print(f"\n  {RED('✗')} Could not delete: {e}\n")
                        continue

        if val.isdigit() and 1 <= int(val) <= len(history[:8]):
            return history[int(val) - 1]["url"]

        return val if val else None


# ---------------------------------------------------------------------------
# Profile Management
# ---------------------------------------------------------------------------

def _profile_load_section(cfg: dict) -> dict:
    try:
        from SpotiFLAC.core.profiles import list_profiles, get_profile
        profiles = list_profiles()
    except Exception:
        return cfg

    if not profiles:
        return cfg

    _section("Load Profile  (optional)")
    print(f"  {DIM('Saved profiles:')}")
    for i, name in enumerate(profiles, 1):
        print(f"    {DIM(f'[{i}]')} {name}")
    print(f"    {DIM('[Enter]')} Start fresh")

    try:
        val = input("  → ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if not val:
        return cfg

    chosen_name: str | None = None
    if val.isdigit() and 1 <= int(val) <= len(profiles):
        chosen_name = profiles[int(val) - 1]
    elif val in profiles:
        chosen_name = val

    if chosen_name:
        profile_data = get_profile(chosen_name)
        if profile_data:
            cfg.update({k: v for k, v in profile_data.items() if not k.startswith("_")})
            print(f"\n  {GREEN('✓')} Profile {BOLD(chosen_name)} loaded.")
    return cfg


def _profile_save_section(cfg: dict) -> None:
    if not _ask_bool("Save this configuration as a profile?", False):
        return
    try:
        from SpotiFLAC.core.profiles import save_profile, list_profiles
    except Exception:
        print(f"  {RED('✗  Profile save unavailable.')}")
        return

    existing = list_profiles()
    if existing:
        print(f"  {DIM('Existing profiles: ' + ', '.join(existing))}")

    name = _ask("Profile name", "default").strip()
    if not name:
        return

    try:
        save_profile(name, cfg)
        print(f"  {GREEN('✓')} Profile {BOLD(name)} saved.")
    except Exception as exc:
        print(f"  {RED(f'✗  Save failed: {exc}')}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _summary(cfg: dict) -> None:
    _section("Configuration Summary")

    def row(label: str, value: str) -> None:
        print(f"  {BOLD(label + ':'): <30} {GREEN(value)}")

    row("URL", cfg["url"][:65])
    row("Output Dir", cfg["output_dir"])

    if cfg.get("output_path"):
        row("Exact File Path", cfg["output_path"])
    row("Services", " → ".join(cfg["services"]))
    row("Quality", cfg["quality"])
    row("Filename format", cfg["filename_format"])

    flags = []
    if cfg["use_track_numbers"]:        flags.append("track-numbers")
    if cfg["use_album_track_numbers"]:  flags.append("album-track-numbers")
    if cfg["use_artist_subfolders"]:    flags.append("artist-subfolders")
    if cfg["use_album_subfolders"]:     flags.append("album-subfolders")
    if cfg["first_artist_only"]:        flags.append("first-artist-only")
    if cfg["include_featuring"]:        flags.append("include-featuring")
    row("Options", ", ".join(flags) if flags else "none")

    row("Lyrics", "enabled (" + ", ".join(cfg["lyrics_providers"]) + ")" if cfg["embed_lyrics"] else "disabled")
    row("Enrichment", "enabled (" + ", ".join(cfg["enrich_providers"]) + ")" if cfg["enrich_metadata"] else "disabled")

    retries = cfg.get("track_max_retries", 0)
    if retries:
        row("Retries per track", str(retries))

    action = cfg.get("post_download_action", "none")
    if action and action != "none":
        row("Post-download", action)

    if cfg.get("qobuz_token"):
        row("Qobuz token", "✓ set")
    if cfg.get("loop"):
        row("Loop", f"every {cfg['loop']} minutes")


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

def run_interactive() -> dict:
    _header()

    # ── Health check ────────────────────────────────────────────────────────
    while True:
        health_status = _display_health_check()

        working_count = sum(health_status.values()) if health_status else 0
        total_services = len(_ALL_SERVICES)

        if working_count == total_services:
            break

        print()
        if not _ask_bool("Some providers are unreachable. Retry health check?", False):
            break

    cfg: dict = {}

    # ── Profile load ────────────────────────────────────────────────────────
    cfg = _profile_load_section(cfg)

    # ── 1. URL ──────────────────────────────────────────────────────────────
    _section("1 · URL")
    print(f"  {DIM('Accepted: Spotify, Apple Music, Tidal, SoundCloud, YouTube, Pandora.')}")
    print(f"  {DIM('Modes: Track, Album, Playlist, Artist Discography.')}")

    prefill = _pick_from_history()

    url = ""
    while True:
        if prefill:
            url = _ask("URL", prefill)
            prefill = None
        else:
            url = _ask("URL")

        if not url:
            print(f"  {RED('⚠  URL is required.')}")
            continue

        lower_url = url.lower()
        is_blocked = False

        if ("youtube.com" in lower_url or "youtu.be" in lower_url) and \
                ("/channel/" in lower_url or "/user/" in lower_url or "/c/" in lower_url or "/@" in lower_url or "/browse/" in lower_url):
            print(f"  {RED('⚠  Discographies are not supported for YouTube.')}")
            print(f"     {DIM('Please provide a Video or Playlist link.')}")
            is_blocked = True

        elif "soundcloud.com" in lower_url:
            path = urlparse(url).path.strip("/")
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1 and parts[0] not in ("discover", "stream", "upload"):
                print(f"  {RED('⚠  Artist profiles are not supported for SoundCloud.')}")
                print(f"     {DIM('Please provide a Track or Set link.')}")
                is_blocked = True

        if not is_blocked:
            break

    cfg["url"] = url

    # ── 2. Output directory ─────────────────────────────────────────────────
    _section("2 · Output Directory")
    try:
        from SpotiFLAC.core.session_memory import get_last_folder
        last_folder = get_last_folder() or "./Downloads"
    except Exception:
        last_folder = "./Downloads"

    cfg["output_dir"] = _ask("Destination folder", last_folder)

    try:
        from SpotiFLAC.core.session_memory import set_last_folder
        set_last_folder(cfg["output_dir"])
    except Exception:
        pass

    # ── 2.5. Custom Output Path (single tracks only) ─────────────────────
    lower_url = url.lower()
    is_single_track = (
            "/track/" in lower_url
            or ("watch?v=" in lower_url and "list=" not in lower_url)
            or ("youtu.be" in lower_url)
            or ("music.apple.com" in lower_url and "?i=" in lower_url)
            or (("soundcloud.com" in lower_url or "on.soundcloud.com" in lower_url) and "/sets/" not in lower_url)
            or ("pandora.com" in lower_url and "/artist/" in lower_url and lower_url.count("/") >= 5)
            or ("pandora.app.link" in lower_url)
    )
    if is_single_track:
        _section("2.5 · Custom Output Path")
        print(f"  {DIM('Specify an exact filename for this single track (optional).')}")
        print(f"  {DIM('Example: my_files/favorite_song.flac')}")

        use_custom = _ask_bool("Set a custom output path?", False)
        if use_custom:
            cfg["output_path"] = _ask("Full file path including extension")
        else:
            cfg["output_path"] = None
    else:
        cfg["output_path"] = None

    # ── 3. Services ──────────────────────────────────────────────────────────
    _section("3 · Audio Services")

    if health_status:
        unavailable = [s for s in _ALL_SERVICES if not health_status.get(s, True)]
        if unavailable:
            print(f"  {YELLOW('⚠  Currently unreachable:')} {', '.join(unavailable)}")

    is_soundcloud_url = "soundcloud.com" in cfg["url"] or "on.soundcloud.com" in cfg["url"]
    is_apple_url      = "music.apple.com" in cfg["url"]
    is_youtube_url    = "youtube.com" in cfg["url"].lower() or "youtu.be" in cfg["url"].lower()
    is_pandora_url    = "pandora.com" in cfg["url"].lower() or "pandora.app.link" in cfg["url"].lower()

    if is_soundcloud_url:
        cfg["services"] = ["soundcloud"]
        print(f"  {GREEN('✓')} Provider {BOLD('soundcloud')} automatically selected.")
    elif is_youtube_url:
        cfg["services"] = ["youtube"]
        print(f"  {GREEN('✓')} Provider {BOLD('youtube')} automatically selected.")
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options  = ["tidal", "qobuz", "deezer", "amazon", "spoti", "apple", "soundcloud"],
                defaults = ["tidal"],
                ordered  = True,
            )
            cfg["services"] = ["youtube"] + fallbacks
    elif is_apple_url:
        cfg["services"] = ["apple"]
        print(f"  {GREEN('✓')} Provider {BOLD('apple')} automatically selected.")
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options  = ["tidal", "qobuz", "deezer", "amazon", "spoti", "youtube"],
                defaults = ["tidal"],
                ordered  = True,
            )
            cfg["services"] = ["apple"] + fallbacks
    elif is_pandora_url:
        cfg["services"] = ["pandora"]
        print(f"  {GREEN('✓')} Provider {BOLD('pandora')} automatically selected.")
        print(f"  {DIM('Note: Pandora delivers MP3 (192kbps default). No lossless streams available.')}")
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options  = ["tidal", "qobuz", "deezer", "amazon", "spoti", "apple"],
                defaults = ["tidal"],
                ordered  = True,
            )
            cfg["services"] = ["pandora"] + fallbacks
    else:
        print(f"  {DIM('Choose services and their priority order (first = highest priority).')}")
        cfg["services"] = _ask_multi(
            "Services (order = priority):",
            options  = ["deezer", "tidal", "qobuz", "amazon", "spoti", "soundcloud", "youtube", "apple", "pandora"],
            defaults = ["tidal"],
            ordered  = True,
        )

    # ── 4. Audio Quality ─────────────────────────────────────────────────────
    _section("4 · Audio Quality")

    if is_soundcloud_url:
        cfg["quality"] = "LOSSLESS"
        cfg["allow_fallback"] = True
        print(f"  {YELLOW('⏭  Skipped:')} {DIM('Only MP3 available')}")
    elif is_pandora_url or (len(cfg["services"]) == 1 and cfg["services"][0] == "pandora"):
        cfg["allow_fallback"] = True
        q_choice = _ask_choice(
            "Pandora Quality:",
            options = ["mp3_192 (High — default)", "aac_64 (Medium)", "aac_32 (Low)"],
            default = "mp3_192 (High — default)",
        )
        cfg["quality"] = q_choice.split(" ")[0]
        print(f"  {DIM('Note: Output will be MP3 or M4A depending on selected quality.')}")
    elif is_youtube_url or (len(cfg["services"]) == 1 and cfg["services"][0] == "youtube"):
        cfg["quality"] = "BEST"
        cfg["allow_fallback"] = True
        print(f"  {YELLOW('⏭  Skipped:')} {DIM('Default Best Audio (Opus/M4A/MP3)')}")
    else:
        print(f"  {DIM('Automatic fallback applies if the requested quality is unavailable.')}")

        has_qobuz  = "qobuz"  in cfg["services"]
        has_tidal  = "tidal"  in cfg["services"]
        has_deezer = "deezer" in cfg["services"]
        has_apple  = "apple"  in cfg["services"]

        if has_qobuz and not (has_tidal or has_deezer or has_apple):
            q_choice = _ask_choice(
                "Qobuz Quality:",
                options = ["6 (CD Lossless)", "7 (Hi-Res)", "27 (Hi-Res Max)"],
                default = "6 (CD Lossless)",
            )
            cfg["quality"] = q_choice.split(" ")[0]
        elif has_tidal and not (has_qobuz or has_deezer or has_apple):
            cfg["quality"] = _ask_choice(
                "Tidal Quality:",
                options = ["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
                default = "LOSSLESS",
            )
        elif has_deezer and not (has_qobuz or has_tidal or has_apple):
            q_choice = _ask_choice(
                "Deezer Quality:",
                options = ["LOSSLESS (FLAC)", "HIGH (MP3 320)", "NORMAL (MP3 128)"],
                default = "LOSSLESS (FLAC)",
            )
            cfg["quality"] = q_choice.split(" ")[0]
        elif has_apple and not (has_qobuz or has_tidal or has_deezer):
            q_choice = _ask_choice(
                "Apple Music Quality:",
                options = ["ALAC (Lossless)", "ATMOS (Spatial)", "AC3", "AAC", "AAC-LEGACY"],
                default = "ALAC (Lossless)",
            )
            cfg["quality"] = q_choice.split(" ")[0].lower()
        elif has_qobuz or has_tidal or has_deezer or has_apple:
            combined_options = [
                "LOSSLESS (FLAC on Deezer/Tidal, '6' on Qobuz, ALAC on Apple)",
                "HI_RES (Best available everywhere, '27' on Qobuz)",
            ]
            if has_apple:
                combined_options.append("ATMOS (Spatial Audio on Apple, HI_RES elsewhere)")
                combined_options.append("AC3 (Dolby Digital on Apple, HIGH elsewhere)")
            if has_tidal:
                combined_options.insert(1, "DOLBY_ATMOS (Dolby Atmos on Tidal, HI_RES elsewhere)")
            if has_qobuz:
                combined_options.append("7 (Hi-Res mid on Qobuz only)")
            combined_options.append("HIGH (MP3 320 / AAC on Apple)")
            if has_apple:
                combined_options.append("AAC-LEGACY (Legacy iTunes on Apple, HIGH elsewhere)")

            q_choice = _ask_choice(
                "Combined Quality:",
                options = combined_options,
                default = combined_options[0],
            )
            if q_choice.startswith("LOSSLESS"):    cfg["quality"] = "LOSSLESS"
            elif q_choice.startswith("HI_RES"):    cfg["quality"] = "HI_RES"
            elif q_choice.startswith("DOLBY_ATMOS"): cfg["quality"] = "DOLBY_ATMOS"
            elif q_choice.startswith("ATMOS"):     cfg["quality"] = "atmos"
            elif q_choice.startswith("AC3"):       cfg["quality"] = "ac3"
            elif q_choice.startswith("7"):         cfg["quality"] = "7"
            elif q_choice.startswith("AAC-LEGACY"):cfg["quality"] = "aac-legacy"
            else:                                   cfg["quality"] = "HIGH"
        else:
            cfg["quality"] = _ask_choice(
                "Quality:",
                options = ["LOSSLESS", "HI_RES", "HIGH"],
                default = "LOSSLESS",
            )

        cfg["allow_fallback"] = _ask_bool("Allow automatic quality fallback?", True)

    # ── 5. Filename format ─────────────────────────────────────────────────
    _section("5 · Filename Format")
    print(f"  {DIM('Placeholders: {title} {artist} {album} {album_artist} {year} {date} {track} {disc} {isrc} {position}')}")
    cfg["filename_format"] = _ask("Format", cfg.get("filename_format", "{title} - {artist}"))

    # ── 6. Organization Options ───────────────────────────────────────────
    _section("6 · Organization Options")

    cfg["use_track_numbers"]      = cfg.get("use_track_numbers", False)
    cfg["use_album_track_numbers"]= cfg.get("use_album_track_numbers", False)
    cfg["use_artist_subfolders"]  = cfg.get("use_artist_subfolders", False)
    cfg["use_album_subfolders"]   = cfg.get("use_album_subfolders", False)
    cfg["first_artist_only"]      = cfg.get("first_artist_only", False)

    cfg["use_track_numbers"] = _ask_bool("Add track number to filename?", cfg["use_track_numbers"])

    if cfg["use_track_numbers"]:
        cfg["use_album_track_numbers"] = _ask_bool("Use original album track number?", cfg["use_album_track_numbers"])
        cfg["use_artist_subfolders"] = False
        cfg["use_album_subfolders"]  = False
        cfg["first_artist_only"]     = False
    else:
        cfg["use_album_track_numbers"] = False
        cfg["use_artist_subfolders"]   = _ask_bool("Create artist subfolders?", cfg["use_artist_subfolders"])
        cfg["use_album_subfolders"]    = _ask_bool("Create album subfolders?", cfg["use_album_subfolders"])
        cfg["first_artist_only"]       = _ask_bool("Use only the first artist in tags and filename?", cfg["first_artist_only"])

    # ── 7. Featuring ─────────────────────────────────────────────────────────
    _section("7 · Featuring")

    lower_url = cfg["url"].lower()
    is_artist_url = (
            "/artist/" in lower_url
            or ("UC" in cfg["url"] and "youtube.com" in lower_url)
    )

    if is_artist_url:
        print(f"  {DIM('Also downloads tracks where the artist appears as a featured artist.')}")
        cfg["include_featuring"] = _ask_bool("Include featuring tracks?", cfg.get("include_featuring", False))
    else:
        print(f"  {YELLOW('⏭  Skipped:')} {DIM('URL is not an artist page.')}")
        cfg["include_featuring"] = False

    # ── 8. Lyrics ────────────────────────────────────────────────────────────
    _section("8 · Lyrics")
    cfg["embed_lyrics"] = _ask_bool("Embed synchronized lyrics?", cfg.get("embed_lyrics", True))

    if cfg["embed_lyrics"]:
        cfg["lyrics_providers"] = _ask_multi(
            "Lyrics providers (order = priority):",
            options  = ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
            defaults = cfg.get("lyrics_providers") or ["spotify", "lrclib", "apple", "amazon"],
            ordered  = True,
        )
    else:
        cfg["lyrics_providers"] = cfg.get("lyrics_providers") or ["spotify", "musixmatch", "lrclib", "apple"]

    # ── 9. Metadata enrichment ──────────────────────────────────────────────
    _section("9 · Metadata Enrichment")
    print(f"  {DIM('Adds genre, BPM, label, HD cover, MusicBrainz IDs, and more.')}")
    cfg["enrich_metadata"] = _ask_bool("Enable metadata enrichment?", cfg.get("enrich_metadata", True))

    if cfg["enrich_metadata"]:
        cfg["enrich_providers"] = _ask_multi(
            "Enrichment providers (order = priority):",
            options  = ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
            defaults = cfg.get("enrich_providers") or ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
            ordered  = True,
        )
    else:
        cfg["enrich_providers"] = cfg.get("enrich_providers") or ["deezer", "apple", "qobuz", "tidal", "soundcloud"]

    # ── 10. Retry ────────────────────────────────────────────────────────────
    _section("10 · Retry on Failure")
    print(f"  {DIM('Extra download attempts per track if all providers fail on first try.')}")
    print(f"  {DIM('Each retry waits exponentially longer (2s, 4s, 8s…).')}")
    default_retries = cfg.get("track_max_retries", 0)
    retry_str = _ask("Extra retries per track (0 = no retry)", str(default_retries))
    try:
        cfg["track_max_retries"] = max(0, int(retry_str))
    except ValueError:
        cfg["track_max_retries"] = 0

    # ── 11. Post-download Action ─────────────────────────────────────────────
    _section("11 · Post-Download Action")
    print(f"  {DIM('Action to perform automatically when all downloads finish.')}")

    action_options = ["none", "open_folder", "notify", "command"]
    default_action = cfg.get("post_download_action", "none")
    action_choice = _ask_choice(
        "Action on completion:",
        options = action_options,
        default = default_action,
    )
    cfg["post_download_action"] = action_choice

    if action_choice == "command":
        print(f"  {DIM('Placeholders: {folder} {succeeded} {failed}')}")
        cfg["post_download_command"] = _ask(
            "Shell command",
            cfg.get("post_download_command", "echo 'Done: {succeeded} tracks in {folder}'"),
        )
    else:
        cfg["post_download_command"] = cfg.get("post_download_command", "")

    # ── 12. Optional Tokens ──────────────────────────────────────────────────
    _section("12 · Optional Tokens")
    cfg["qobuz_token"] = _ask("Qobuz auth token (leave blank to skip)", "") or None

    # ── 13. Loop ─────────────────────────────────────────────────────────────
    loop_str = _ask("Repeat every N minutes (leave blank to disable)", "")
    cfg["loop"] = int(loop_str) if loop_str.isdigit() else None

    # ── Profile save ────────────────────────────────────────────────────────
    _profile_save_section(cfg)

    # ── Summary + confirmation ───────────────────────────────────────────────
    _summary(cfg)
    print()
    if not _ask_bool(BOLD("Start download with this configuration?"), True):
        print(f"\n  {YELLOW('Operation cancelled.')}\n")
        sys.exit(0)

    _section("Equivalent CLI command")
    _print_cli_command(cfg)

    return cfg


def _print_cli_command(cfg: dict) -> None:
    parts = [f'spotiflac "{cfg["url"]}" "{cfg["output_dir"]}"']
    if cfg.get("output_path"):
        parts.append(f'-o "{cfg["output_path"]}"')
    parts.append(f'-s {" ".join(cfg["services"])}')
    if cfg["quality"] not in ("LOSSLESS", "BEST"):
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
    if not cfg["enrich_metadata"]:
        parts.append("--no-enrich")
    else:
        parts.append(f'--enrich-providers {" ".join(cfg["enrich_providers"])}')
    if cfg.get("track_max_retries"):
        parts.append(f'--retries {cfg["track_max_retries"]}')
    if cfg.get("post_download_action") and cfg["post_download_action"] != "none":
        parts.append(f'--post-action {cfg["post_download_action"]}')
        if cfg["post_download_action"] == "command" and cfg.get("post_download_command"):
            parts.append(f'--post-command "{cfg["post_download_command"]}"')
    if cfg.get("qobuz_token"):
        parts.append(f'--qobuz-token "{cfg["qobuz_token"]}"')
    if cfg.get("loop"):
        parts.append(f'--loop {cfg["loop"]}')

    cmd = " \\\n    ".join(parts)
    print(f"\n  {DIM(cmd)}\n")
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
import asyncio

from .core.health_check import run_health_check
from .core.quality import normalize_quality

_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def BOLD(t):
    return _c("1", t)


def DIM(t):
    return _c("2", t)


def CYAN(t):
    return _c("96", t)


def GREEN(t):
    return _c("92", t)


def YELLOW(t):
    return _c("93", t)


def RED(t):
    return _c("91", t)


def BLUE(t):
    return _c("94", t)


def MAGENTA(t):
    return _c("95", t)


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
    print(
        f"    {DIM('Enter numbers separated by space (e.g., 1 3 2) — Enter = default')}"
    )
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
        result = [
            options[int(t) - 1]
            for t in tokens
            if t.isdigit() and 1 <= int(t) <= len(options)
        ]
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

_ALL_SERVICES = [
    "tidal",
    "qobuz",
    "deezer",
    "amazon",
    "soundcloud",
    "apple",
    "youtube",
    "pandora",
    "joox",
    "netease",
    "migu",
    "kuwo",
]


async def _display_health_check() -> dict[str, bool]:
    _section("Service Availability Check")
    print(f"  {DIM('Probing endpoints...')} ", end="", flush=True)

    try:
        results = await run_health_check(_ALL_SERVICES, include_all_endpoints=True)
    except Exception as e:
        print(f"\r  {RED(f'Health check error: {e}')}" + " " * 20)
        results = []

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
    summary_color = (
        GREEN if working_count == total else (YELLOW if working_count > 0 else RED)
    )
    print(f"\n  {summary_color(f'{working_count}/{total} providers reachable')}")

    if working_count == 0:
        print(
            f"\n  {RED('✗  No providers reachable — check your internet connection.')}"
        )

    return status


# ---------------------------------------------------------------------------
# URL History
# ---------------------------------------------------------------------------


async def _pick_from_history() -> str | None:
    try:
        from .core.session_memory import (
            get_url_history_async,
            remove_url_from_history_async,
            clear_url_history_async,
        )
        from .core.history import clear_recent_fetches
    except Exception:
        return None

    while True:
        history = await get_url_history_async()

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
        print(f"    {DIM('[c]')} Clear URL history")
        print(f"    {DIM('[r]')} Clear recent fetches")

        try:
            val = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if not val:
            return None

        val_lower = val.lower()

        if val_lower in ("c", "clear"):
            if _ask_bool("Are you sure you want to clear URL history?", False):
                try:
                    await clear_url_history_async()
                    print(f"\n  {GREEN('✓')} URL history cleared.\n")
                    continue
                except Exception as e:
                    print(f"\n  {RED('✗')} Could not clear URL history: {e}\n")
                    continue
            else:
                print()
                continue

        if val_lower in ("r", "recent", "clear recent"):
            if _ask_bool("Are you sure you want to clear recent fetches?", False):
                try:
                    # Delegate blocking sync core/history I/O to to_thread for safety
                    await asyncio.to_thread(clear_recent_fetches)
                    print(f"\n  {GREEN('✓')} Recent fetches cleared.\n")
                    continue
                except Exception as e:
                    print(f"\n  {RED('✗')} Could not clear recent fetches: {e}\n")
                    continue
            else:
                print()
                continue

        if val_lower.startswith("d") and len(val_lower) > 1:
            num_str = val_lower[1:].strip()
            if num_str.isdigit():
                idx = int(num_str) - 1
                if 0 <= idx < len(history[:8]):
                    url_to_remove = history[idx].get("url")
                    try:
                        await remove_url_from_history_async(url_to_remove)
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


async def _profile_load_section(cfg: dict) -> dict:
    try:
        from .core.profiles import (
            list_profiles_async,
            get_profile_async,
            delete_profile_async,
        )
    except Exception:
        return cfg

    while True:
        profiles = await list_profiles_async()
        if not profiles:
            return cfg

        _section("Load Profile  (optional)")
        print(f"  {DIM('Saved profiles:')}")
        for i, name in enumerate(profiles, 1):
            print(f"    {DIM(f'[{i}]')} {name}")

        print(f"\n    {DIM('[Enter]')} Start fresh")
        print(f"    {DIM('[d + num]')} Delete a profile (e.g., d1, d2)")

        try:
            val = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if not val:
            return cfg

        val_lower = val.lower()

        if val_lower.startswith("d") and len(val_lower) > 1:
            num_str = val_lower[1:].strip()
            if num_str.isdigit():
                idx = int(num_str) - 1
                if 0 <= idx < len(profiles):
                    prof_to_delete = profiles[idx]
                    if _ask_bool(f"Delete profile '{prof_to_delete}'?", False):
                        await delete_profile_async(prof_to_delete)
                        print(
                            f"\n  {GREEN('✓')} Profile {BOLD(prof_to_delete)} deleted.\n"
                        )
                    continue

        chosen_name: str | None = None
        if val.isdigit() and 1 <= int(val) <= len(profiles):
            chosen_name = profiles[int(val) - 1]
        elif val in profiles:
            chosen_name = val

        if chosen_name:
            profile_data = await get_profile_async(chosen_name)
            if profile_data:
                cfg.update(
                    {k: v for k, v in profile_data.items() if not k.startswith("_")}
                )
                print(f"\n  {GREEN('✓')} Profile {BOLD(chosen_name)} loaded.")
            return cfg


async def _profile_save_section(cfg: dict) -> None:
    if not _ask_bool("Save this configuration as a profile?", False):
        return
    try:
        from .core.profiles import save_profile_async, list_profiles_async
    except Exception:
        print(f"  {RED('✗  Profile save unavailable.')}")
        return

    existing = await list_profiles_async()
    if existing:
        print(f"  {DIM('Existing profiles: ' + ', '.join(existing))}")

    name = _ask("Profile name", "default").strip()
    if not name:
        return

    try:
        await save_profile_async(name, cfg)
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
    if cfg["use_track_numbers"]:
        flags.append("track-numbers")
    if cfg["use_album_track_numbers"]:
        flags.append("album-track-numbers")
    if cfg["use_artist_subfolders"]:
        flags.append("artist-subfolders")
    if cfg["use_album_subfolders"]:
        flags.append("album-subfolders")
    if cfg["first_artist_only"]:
        flags.append("first-artist-only")
    row("Options", ", ".join(flags) if flags else "none")

    row(
        "Lyrics",
        (
            "enabled (" + ", ".join(cfg["lyrics_providers"]) + ")"
            if cfg["embed_lyrics"]
            else "disabled"
        ),
    )
    row(
        "Enrichment",
        (
            "enabled (" + ", ".join(cfg["enrich_providers"]) + ")"
            if cfg["enrich_metadata"]
            else "disabled"
        ),
    )

    retries = cfg.get("track_max_retries", 0)
    if retries:
        row("Retries per track", str(retries))

    timeout = cfg.get("timeout_s", 0)
    if timeout:
        row("Timeout", f"{timeout} seconds")

    action = cfg.get("post_download_action", "none")
    if action and action != "none":
        row("Post-download", action)

    if cfg.get("qobuz_local_api_url"):
        row("Qobuz local API", cfg["qobuz_local_api_url"])
    if cfg.get("tidal_custom_api"):
        row("Custom Tidal API", cfg["tidal_custom_api"])
    if cfg.get("loop"):
        row("Loop", f"every {cfg['loop']} minutes")


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------


async def run_interactive() -> dict:
    _header()

    # ── Health check ────────────────────────────────────────────────────────
    while True:
        health_status = await _display_health_check()

        working_count = sum(health_status.values()) if health_status else 0
        total_services = len(_ALL_SERVICES)

        if working_count == total_services:
            break

        print()
        if not _ask_bool("Some providers are unreachable. Retry health check?", False):
            break

    cfg: dict = {}

    # ── Profile load ────────────────────────────────────────────────────────
    cfg = await _profile_load_section(cfg)

    # ── 1. URL ──────────────────────────────────────────────────────────────
    _section("1 · URL")
    print(
        f"  {DIM('Accepted: Spotify, Apple Music, Tidal, SoundCloud, YouTube, Pandora.')}"
    )
    print(f"  {DIM('Modes: Track, Album, Playlist, Artist Discography.')}")

    prefill = await _pick_from_history()

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

        if ("youtube.com" in lower_url or "youtu.be" in lower_url) and (
            "/channel/" in lower_url
            or "/user/" in lower_url
            or "/c/" in lower_url
            or "/@" in lower_url
            or "/browse/" in lower_url
        ):
            print(f"  {RED('⚠  Discographies are not supported for YouTube.')}")
            print(f"     {DIM('Please provide a Video or Playlist link.')}")
            is_blocked = True

        elif "soundcloud.com" in lower_url:
            path = urlparse(url).path.strip("/")
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1 and parts[0] not in ("discover", "stream", "upload"):
                print(
                    f"  {RED('⚠  Artist profiles are not supported for SoundCloud.')}"
                )
                print(f"     {DIM('Please provide a Track or Set link.')}")
                is_blocked = True

        if not is_blocked:
            break

    cfg["url"] = url

    # ── 2. Output directory ─────────────────────────────────────────────────
    _section("2 · Output Directory")
    try:
        from .core.session_memory import get_last_folder_async

        last_folder = await get_last_folder_async() or "./Downloads"
    except Exception:
        last_folder = "./Downloads"

    cfg["output_dir"] = _ask("Destination folder", cfg.get("output_dir", last_folder))

    try:
        from .core.session_memory import set_last_folder_async

        await set_last_folder_async(cfg["output_dir"])
    except Exception:
        pass

    # ── 2.5. Custom Output Path (single tracks only) ─────────────────────
    lower_url = url.lower()
    is_single_track = (
        "/track/" in lower_url
        or ("watch?v=" in lower_url and "list=" not in lower_url)
        or ("youtu.be" in lower_url)
        or ("music.apple.com" in lower_url and "?i=" in lower_url)
        or (
            ("soundcloud.com" in lower_url or "on.soundcloud.com" in lower_url)
            and "/sets/" not in lower_url
        )
        or (
            "pandora.com" in lower_url
            and "/artist/" in lower_url
            and lower_url.count("/") >= 5
        )
        or ("pandora.app.link" in lower_url)
    )
    if is_single_track:
        _section("2.5 · Custom Output Path")
        print(f"  {DIM('Specify an exact filename for this single track (optional).')}")
        print(f"  {DIM('Example: my_files/favorite_song.flac')}")

        use_custom = _ask_bool(
            "Set a custom output path?", bool(cfg.get("output_path"))
        )
        if use_custom:
            cfg["output_path"] = _ask(
                "Full file path including extension", cfg.get("output_path", "")
            )
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

    is_soundcloud_url = (
        "soundcloud.com" in cfg["url"] or "on.soundcloud.com" in cfg["url"]
    )
    is_apple_url = "music.apple.com" in cfg["url"]
    is_youtube_url = (
        "youtube.com" in cfg["url"].lower() or "youtu.be" in cfg["url"].lower()
    )
    is_pandora_url = (
        "pandora.com" in cfg["url"].lower() or "pandora.app.link" in cfg["url"].lower()
    )

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
                options=["tidal", "qobuz", "deezer", "amazon", "apple", "soundcloud"],
                defaults=["tidal"],
                ordered=True,
            )
            cfg["services"] = ["youtube"] + fallbacks
    elif is_apple_url:
        cfg["services"] = ["apple"]
        print(f"  {GREEN('✓')} Provider {BOLD('apple')} automatically selected.")
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options=["tidal", "qobuz", "deezer", "amazon", "youtube"],
                defaults=["tidal"],
                ordered=True,
            )
            cfg["services"] = ["apple"] + fallbacks
    elif is_pandora_url:
        cfg["services"] = ["pandora"]
        print(f"  {GREEN('✓')} Provider {BOLD('pandora')} automatically selected.")
        print(
            f"  {DIM('Note: Pandora delivers MP3 (192kbps default). No lossless streams available.')}"
        )
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options=["tidal", "qobuz", "deezer", "amazon", "apple"],
                defaults=["tidal"],
                ordered=True,
            )
            cfg["services"] = ["pandora"] + fallbacks
    else:
        print(
            f"  {DIM('Choose services and their priority order (first = highest priority).')}"
        )
        cfg["services"] = _ask_multi(
            "Services (order = priority):",
            options=[
                "deezer",
                "tidal",
                "qobuz",
                "amazon",
                "joox",
                "netease",
                "migu",
                "kuwo",
                "soundcloud",
                "youtube",
                "apple",
                "pandora",
            ],
            defaults=cfg.get("services", ["tidal"]),
            ordered=True,
        )

    # ── 4. Audio Quality ─────────────────────────────────────────────────────
    _section("4 · Audio Quality")

    if is_soundcloud_url:
        cfg["quality"] = "LOSSLESS"
        cfg["allow_fallback"] = True
        print(f"  {YELLOW('⏭  Skipped:')} {DIM('Only MP3 available')}")
    elif is_pandora_url or (
        len(cfg["services"]) == 1 and cfg["services"][0] == "pandora"
    ):
        cfg["allow_fallback"] = True
        q_choice = _ask_choice(
            "Pandora Quality:",
            options=["mp3_192 (High — default)", "aac_64 (Medium)", "aac_32 (Low)"],
            default="mp3_192 (High — default)",
        )
        # Pandora uses provider-specific tokens directly
        cfg["quality"] = q_choice.split(" ")[0]
        print(
            f"  {DIM('Note: Output will be MP3 or M4A depending on selected quality.')}"
        )
    elif is_youtube_url or (
        len(cfg["services"]) == 1 and cfg["services"][0] == "youtube"
    ):
        cfg["quality"] = "BEST"
        cfg["allow_fallback"] = True
        print(f"  {YELLOW('⏭  Skipped:')} {DIM('Default Best Audio (Opus/M4A/MP3)')}")
    else:
        print(
            f"  {DIM('Automatic fallback applies if the requested quality is unavailable.')}"
        )

        has_qobuz = "qobuz" in cfg["services"]
        has_tidal = "tidal" in cfg["services"]
        has_deezer = "deezer" in cfg["services"]
        has_apple = "apple" in cfg["services"]

        if has_qobuz and not (has_tidal or has_deezer or has_apple):
            q_default = {
                "6": "6 (CD Lossless)",
                "7": "7 (Hi-Res)",
                "27": "27 (Hi-Res Max)",
            }.get(str(cfg.get("quality", "6") or "6"), "6 (CD Lossless)")
            q_choice = _ask_choice(
                "Qobuz Quality:",
                options=["6 (CD Lossless)", "7 (Hi-Res)", "27 (Hi-Res Max)"],
                default=q_default,
            )
            cfg["quality"] = normalize_quality(q_choice.split(" ")[0])
        elif has_tidal and not (has_qobuz or has_deezer or has_apple):
            tidal_default = str(cfg.get("quality", "LOSSLESS") or "LOSSLESS").upper()
            if tidal_default not in [
                "DOLBY_ATMOS",
                "HI_RES_LOSSLESS",
                "LOSSLESS",
                "HIGH",
                "LOW",
            ]:
                tidal_default = "LOSSLESS"
            q = _ask_choice(
                "Tidal Quality:",
                options=["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
                default=tidal_default,
            )
            cfg["quality"] = normalize_quality(q)
        elif has_deezer and not (has_qobuz or has_tidal or has_apple):
            deezer_default = {
                "LOSSLESS": "LOSSLESS (FLAC)",
                "HIGH": "HIGH (MP3 320)",
                "NORMAL": "NORMAL (MP3 128)",
            }.get(
                str(cfg.get("quality", "LOSSLESS") or "LOSSLESS").upper(),
                "LOSSLESS (FLAC)",
            )
            q_choice = _ask_choice(
                "Deezer Quality:",
                options=["LOSSLESS (FLAC)", "HIGH (MP3 320)", "NORMAL (MP3 128)"],
                default=deezer_default,
            )
            cfg["quality"] = normalize_quality(q_choice.split(" ")[0])
        elif has_apple and not (has_qobuz or has_tidal or has_deezer):
            apple_default = {
                "ALAC": "ALAC (Lossless)",
                "ATMOS": "ATMOS (Spatial)",
                "AC3": "AC3",
                "AAC": "AAC",
                "AAC-LEGACY": "AAC-LEGACY",
            }.get(str(cfg.get("quality", "ALAC") or "ALAC").upper(), "ALAC (Lossless)")
            q_choice = _ask_choice(
                "Apple Music Quality:",
                options=[
                    "ALAC (Lossless)",
                    "ATMOS (Spatial)",
                    "AC3",
                    "AAC",
                    "AAC-LEGACY",
                ],
                default=apple_default,
            )
            cfg["quality"] = normalize_quality(q_choice.split(" ")[0])
        elif has_qobuz or has_tidal or has_deezer or has_apple:
            combined_options = [
                "LOSSLESS (FLAC on Deezer/Tidal, '6' on Qobuz, ALAC on Apple)",
                "HI_RES (Best available everywhere, '27' on Qobuz)",
            ]
            if has_apple:
                combined_options.append(
                    "ATMOS (Spatial Audio on Apple, HI_RES elsewhere)"
                )
                combined_options.append("AC3 (Dolby Digital on Apple, HIGH elsewhere)")
            if has_tidal:
                combined_options.insert(
                    1, "DOLBY_ATMOS (Dolby Atmos on Tidal, HI_RES elsewhere)"
                )
            if has_qobuz:
                combined_options.append("7 (Hi-Res mid on Qobuz only)")
            combined_options.append("HIGH (MP3 320 / AAC on Apple)")
            if has_apple:
                combined_options.append(
                    "AAC-LEGACY (Legacy iTunes on Apple, HIGH elsewhere)"
                )

            quality_key = str(cfg.get("quality", "LOSSLESS") or "LOSSLESS").upper()
            default_combined = next(
                (opt for opt in combined_options if opt.startswith(quality_key)),
                combined_options[0],
            )
            q_choice = _ask_choice(
                "Combined Quality:",
                options=combined_options,
                default=default_combined,
            )
            if q_choice.startswith("LOSSLESS"):
                cfg["quality"] = "LOSSLESS"
            elif q_choice.startswith("HI_RES"):
                cfg["quality"] = "HI_RES"
            elif q_choice.startswith("DOLBY_ATMOS"):
                cfg["quality"] = "DOLBY_ATMOS"
            elif q_choice.startswith("ATMOS"):
                cfg["quality"] = "atmos"
            elif q_choice.startswith("AC3"):
                cfg["quality"] = "ac3"
            elif q_choice.startswith("7"):
                cfg["quality"] = "7"
            elif q_choice.startswith("AAC-LEGACY"):
                cfg["quality"] = "aac-legacy"
            else:
                cfg["quality"] = "HIGH"
            # normalize combined choice to canonical form
            cfg["quality"] = normalize_quality(cfg["quality"])
        else:
            q = _ask_choice(
                "Quality:",
                options=["LOSSLESS", "HI_RES", "HIGH"],
                default="LOSSLESS",
            )
            cfg["quality"] = normalize_quality(q)

        cfg["allow_fallback"] = _ask_bool(
            "Allow automatic quality fallback?", cfg.get("allow_fallback", True)
        )

    # ── 5. Filename format ─────────────────────────────────────────────────
    _section("5 · Filename Format")
    print(
        f"  {DIM('Placeholders: {title} {artist} {album} {album_artist} {year} {date} {track} {disc} {isrc} {position}')}"
    )
    cfg["filename_format"] = _ask(
        "Format", cfg.get("filename_format", "{title} - {artist}")
    )

    # ── 6. Organization Options ───────────────────────────────────────────
    _section("6 · Organization Options")

    cfg["use_track_numbers"] = cfg.get("use_track_numbers", False)
    cfg["use_album_track_numbers"] = cfg.get("use_album_track_numbers", False)
    cfg["use_artist_subfolders"] = cfg.get("use_artist_subfolders", False)
    cfg["use_album_subfolders"] = cfg.get("use_album_subfolders", False)
    cfg["first_artist_only"] = cfg.get("first_artist_only", False)

    cfg["use_track_numbers"] = _ask_bool(
        "Add track number to filename?", cfg["use_track_numbers"]
    )

    if cfg["use_track_numbers"]:
        cfg["use_album_track_numbers"] = _ask_bool(
            "Use original album track number?", cfg["use_album_track_numbers"]
        )
        cfg["use_artist_subfolders"] = False
        cfg["use_album_subfolders"] = False
        cfg["first_artist_only"] = False
    else:
        cfg["use_album_track_numbers"] = False
        cfg["use_artist_subfolders"] = _ask_bool(
            "Create artist subfolders?", cfg["use_artist_subfolders"]
        )
        cfg["use_album_subfolders"] = _ask_bool(
            "Create album subfolders?", cfg["use_album_subfolders"]
        )
        cfg["first_artist_only"] = _ask_bool(
            "Use only the first artist in tags and filename?", cfg["first_artist_only"]
        )

    # ── 7. Lyrics ────────────────────────────────────────────────────────────
    _section("7 · Lyrics")
    cfg["embed_lyrics"] = _ask_bool(
        "Embed synchronized lyrics?", cfg.get("embed_lyrics", True)
    )

    if cfg["embed_lyrics"]:
        cfg["lyrics_providers"] = _ask_multi(
            "Lyrics providers (order = priority):",
            options=["spotify", "apple", "musixmatch", "lrclib", "amazon"],
            defaults=cfg.get("lyrics_providers") or ["lrclib", "apple", "amazon"],
            ordered=True,
        )
    else:
        cfg["lyrics_providers"] = cfg.get("lyrics_providers") or [
            "lrclib",
            "apple",
            "amazon",
        ]

    # ── 8. Metadata enrichment ──────────────────────────────────────────────
    _section("8 · Metadata Enrichment")
    print(f"  {DIM('Adds genre, BPM, label, HD cover, MusicBrainz IDs, and more.')}")
    cfg["enrich_metadata"] = _ask_bool(
        "Enable metadata enrichment?", cfg.get("enrich_metadata", True)
    )

    if cfg["enrich_metadata"]:
        cfg["enrich_providers"] = _ask_multi(
            "Enrichment providers (order = priority):",
            options=["deezer", "apple", "qobuz", "tidal", "soundcloud"],
            defaults=cfg.get("enrich_providers")
            or ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
            ordered=True,
        )
    else:
        cfg["enrich_providers"] = cfg.get("enrich_providers") or [
            "deezer",
            "apple",
            "qobuz",
            "tidal",
            "soundcloud",
        ]

    # ── 9. Retry ────────────────────────────────────────────────────────────
    _section("9 · Retry on Failure")
    print(
        f"  {DIM('Extra download attempts per track if all providers fail on first try.')}"
    )
    print(f"  {DIM('Each retry waits exponentially longer (2s, 4s, 8s…).')}")
    default_retries = cfg.get("track_max_retries", 0)
    retry_str = _ask("Extra retries per track (0 = no retry)", str(default_retries))
    try:
        cfg["track_max_retries"] = max(0, int(retry_str))
    except ValueError:
        cfg["track_max_retries"] = 0

    # ── 9.5. Timeout ───────────────────────────────────────────────────
    _section("9.5 · Download Timeout")
    print(f"  {DIM('Maximum time allowed for a single track download attempt.')}")
    print(f"  {DIM('Useful to prevent hanging if a provider gets stuck.')}")
    default_timeout = cfg.get("timeout_s", 0)
    timeout_str = _ask(
        "Timeout per track in seconds (0 = disabled)", str(default_timeout)
    )
    try:
        cfg["timeout_s"] = max(0, int(timeout_str))
    except ValueError:
        cfg["timeout_s"] = 0

    # ── 10. Post-download Action ─────────────────────────────────────────────
    _section("10 · Post-Download Action")
    print(f"  {DIM('Action to perform automatically when all downloads finish.')}")

    action_options = ["none", "open_folder", "notify", "command"]
    default_action = cfg.get("post_download_action", "none")
    action_choice = _ask_choice(
        "Action on completion:",
        options=action_options,
        default=default_action,
    )
    cfg["post_download_action"] = action_choice

    if action_choice == "command":
        print(f"  {DIM('Placeholders: {folder} {succeeded} {failed}')}")
        cfg["post_download_command"] = _ask(
            "Shell command",
            cfg.get(
                "post_download_command", "echo 'Done: {succeeded} tracks in {folder}'"
            ),
        )
    else:
        cfg["post_download_command"] = cfg.get("post_download_command", "")

    # ── 11. Optional Qobuz Local API ───────────────────────────────────────────────
    _section("11 · Optional Qobuz Local API")
    print(
        f"  {DIM('Self-host your own qobuz-api instance for guaranteed availability.')}"
    )
    print(
        f"  {DIM('For a self-hosted Qobuz API, visit: https://github.com/BartolomeoRusso9/qobuz-api')}"
    )
    cfg["qobuz_local_api_url"] = (
        _ask(
            "Qobuz local API URL (leave blank to skip)",
            cfg.get("qobuz_local_api_url", "") or "",
        )
        or None
    )

    # ── 11.5. Custom Tidal API ───────────────────────────────────────────────
    print(
        f"  {DIM('Self-host your own hifi-api instance for guaranteed availability.')}"
    )
    print(f"  {DIM('Create one at: https://github.com/binimum/hifi-api')}")
    cfg["tidal_custom_api"] = (
        _ask(
            "Custom Tidal API URL (leave blank to skip)",
            cfg.get("tidal_custom_api", "") or "",
        )
        or None
    )

    # ── 12. Loop ─────────────────────────────────────────────────────────────
    loop_str = _ask(
        "Repeat every N minutes (leave blank to disable)", str(cfg.get("loop", ""))
    )
    cfg["loop"] = int(loop_str) if loop_str.isdigit() else None

    # ── Profile save ────────────────────────────────────────────────────────
    await _profile_save_section(cfg)

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
    if cfg["use_track_numbers"]:
        parts.append("--use-track-numbers")
    if cfg["use_album_track_numbers"]:
        parts.append("--use-album-track-numbers")
    if cfg["use_artist_subfolders"]:
        parts.append("--use-artist-subfolders")
    if cfg["use_album_subfolders"]:
        parts.append("--use-album-subfolders")
    if cfg["first_artist_only"]:
        parts.append("--first-artist-only")
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
    if cfg.get("timeout_s"):
        parts.append(f'--timeout {cfg["timeout_s"]}')
    if cfg.get("post_download_action") and cfg["post_download_action"] != "none":
        parts.append(f'--post-action {cfg["post_download_action"]}')
        if cfg["post_download_action"] == "command" and cfg.get(
            "post_download_command"
        ):
            parts.append(f'--post-command "{cfg["post_download_command"]}"')
    if cfg.get("qobuz_local_api_url"):
        parts.append(f'--qobuz-local-api "{cfg["qobuz_local_api_url"]}"')
    if cfg.get("tidal_custom_api"):
        parts.append(f'--tidal-api "{cfg["tidal_custom_api"]}"')
    if cfg.get("loop"):
        parts.append(f'--loop {cfg["loop"]}')

    cmd = " \\\n    ".join(parts)
    print(f"\n  {DIM(cmd)}\n")

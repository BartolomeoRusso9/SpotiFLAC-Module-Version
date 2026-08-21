"""SpotiFLAC — Interactive Mode.
New features compared to previous version:
  - Automatic health check at startup
  - URL history with quick selection
  - Last output folder as default
  - Profile management (load / save)
  - Per-track retry section
  - Post-download actions section.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import sys
from urllib.parse import urlparse

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
        sys.exit(0)
    return val or default


def _ask_bool(prompt: str, default: bool = False) -> bool:
    hint = DIM("Y/n" if default else "y/N")
    try:
        val = input(f"  {prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.exit(0)
    if not val:
        return default
    return val in ("y", "yes", "s", "si", "1")


def _ask_choice(prompt: str, options: list[str], default: str) -> str:
    print(f"  {prompt}")
    for _i, opt in enumerate(options, 1):
        marker = GREEN("▶") if opt == default else " "
        print(f"    {marker} {_i}. {opt}")
    try:
        val = input("  → ").strip()
    except (EOFError, KeyboardInterrupt):
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
    print(f"  {prompt}")
    for _i, opt in enumerate(options, 1):
        marker = GREEN("●") if opt in defaults else DIM("○")
        def_hint = DIM(" (default)") if opt in defaults else ""
        print(f"    {marker} {_i}. {opt}{def_hint}")
    try:
        val = input("  → ").strip()
    except (EOFError, KeyboardInterrupt):
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
        return result or list(defaults)
    result = [
        options[int(t) - 1]
        for t in tokens
        if t.isdigit() and 1 <= int(t) <= len(options)
    ]
    return result or list(defaults)


def _section(title: str) -> None:
    print(f"\n{BOLD(CYAN(title))}")
    print(DIM("─" * 40))


def _header() -> None:
    print(f"\n{BOLD(MAGENTA('SpotiFLAC — Interactive Mode'))}")
    print(DIM("=" * 40))


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

_ALL_SERVICES = [
    "apple",
    "lrclib",
    "musixmatch",
    "spotify",
    "amazon",
    "deezer",
    "genius",
    "netease",
    "qq",
    "youtube",
    "kugou",
]

_SERVICE_LABELS = {
    "apple": "Apple Music (lyrics)",
    "lrclib": "LRCLIB (lyrics)",
    "musixmatch": "Musixmatch (lyrics)",
    "spotify": "Spotify (lyrics)",
    "amazon": "Amazon Music (lyrics)",
    "deezer": "Deezer (lyrics)",
    "genius": "Genius (lyrics)",
    "netease": "NetEase (lyrics)",
    "qq": "QQ Music (lyrics)",
    "youtube": "YouTube (lyrics)",
    "kugou": "Kugou (lyrics)",
}


async def _display_health_check() -> dict[str, bool]:
    _section("Lyrics Providers Availability Check")
    print(
        DIM(
            "  These checks cover lyric sources used for embedding lyrics. "
            "They are not audio download providers and a failed check does not block downloads."
        )
    )

    try:
        results = await run_health_check(
            _ALL_SERVICES,
            include_all_endpoints=True,
        )
    except Exception:
        results = []

    if not results:
        return {}

    status = dict.fromkeys(_ALL_SERVICES, False)
    for r in results:
        if r.ok:
            status[r.provider] = True

    for svc in _ALL_SERVICES:
        ok = status[svc]
        icon = GREEN("✅") if ok else RED("❌")
        print(f"  {icon} {_SERVICE_LABELS[svc]}")

    working_count = sum(status.values())
    total_services = len(_ALL_SERVICES)
    print(f"\n  {BOLD('Total reachable:')} {working_count}/{total_services}")

    if working_count == 0:
        print(f"  {RED('Warning: All primary services are currently unreachable!')}")

    return status


# ---------------------------------------------------------------------------
# URL History
# ---------------------------------------------------------------------------


async def _pick_from_history() -> str | None:
    try:
        from .core.history import clear_recent_fetches
        from .core.session_memory import (
            clear_url_history_async,
            get_url_history_async,
            remove_url_from_history_async,
        )
    except Exception:
        return None

    while True:
        history = await get_url_history_async()

        if not history:
            return None

        _section("Recent URLs  (optional)")

        for _i, entry in enumerate(history[:8], 1):
            label = entry.get("label", entry.get("url", ""))[:55]
            url_short = entry.get("url", "")[:60]
            if label != url_short:
                print(f"  {_i}. {label}")
                print(f"     {DIM(url_short)}")
            else:
                print(f"  {_i}. {url_short}")

        print(
            DIM(
                "\n  [1-8] Select  |  c: Clear history  |  r: Clear recent  |  d<num>: Delete entry"
            )
        )

        try:
            val = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)

        if not val:
            return None

        val_lower = val.lower()

        if val_lower in ("c", "clear"):
            if _ask_bool("Are you sure you want to clear URL history?", False):
                try:
                    await clear_url_history_async()
                    continue
                except Exception:
                    continue
            else:
                continue

        if val_lower in ("r", "recent", "clear recent"):
            if _ask_bool("Are you sure you want to clear recent fetches?", False):
                try:
                    await asyncio.to_thread(clear_recent_fetches)
                    continue
                except Exception:
                    continue
            else:
                continue

        if val_lower.startswith("d") and len(val_lower) > 1:
            num_str = val_lower[1:].strip()
            if num_str.isdigit():
                idx = int(num_str) - 1
                if 0 <= idx < len(history[:8]):
                    url_to_remove = history[idx].get("url")
                    try:
                        await remove_url_from_history_async(url_to_remove)
                        continue
                    except Exception:
                        continue

        if val.isdigit() and 1 <= int(val) <= len(history[:8]):
            return history[int(val) - 1]["url"]

        return val or None


# ---------------------------------------------------------------------------
# Extension Registries
# ---------------------------------------------------------------------------

_REGISTRY_SOURCE_LABELS = {
    "environment": "terminal export",
    "env_file": ".env file",
    "custom": "added here",
}


def _print_registries(registries: list[dict]) -> None:
    if not registries:
        print(DIM("  No registry links configured."))
        return
    for _i, r in enumerate(registries, 1):
        sources = ", ".join(
            _REGISTRY_SOURCE_LABELS.get(s, s) for s in r.get("sources", [])
        )
        state = GREEN("enabled") if r.get("enabled") else RED("removed")
        print(f"  {_i}. {r['url']}")
        print(f"     {DIM(f'source: {sources}  ·  {state}')}")


async def _manage_registries_section() -> None:
    """Lets the user inspect, add, or remove extension-registry links.

    Mirrors the same management available from the GUI Settings → Extensions
    tab: shows links coming from a terminal export, a .env file, or added
    previously from this menu, and lets the user delete any of them.
    """
    try:
        from .extensions import registry_config
    except Exception:
        return

    while True:
        registries = await asyncio.to_thread(registry_config.list_registries)

        _section("Extension Registries  (optional)")
        _print_registries(registries)
        print(DIM("\n  Enter: Continue  |  a: Add link  |  d<num>: Remove link"))

        try:
            val = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)

        if not val:
            return

        val_lower = val.lower()

        if val_lower in ("a", "add"):
            url = _ask("Registry URL (https://...)").strip()
            if url:
                try:
                    await asyncio.to_thread(registry_config.add_registry, url)
                except Exception as e:
                    print(f"  {RED('Unable to add registry:')} {e}")
            continue

        if val_lower.startswith("d") and len(val_lower) > 1:
            num_str = val_lower[1:].strip()
            if num_str.isdigit():
                idx = int(num_str) - 1
                if 0 <= idx < len(registries):
                    url_to_remove = registries[idx]["url"]
                    try:
                        await asyncio.to_thread(
                            registry_config.remove_registry, url_to_remove
                        )
                    except Exception as e:
                        print(f"  {RED('Unable to remove registry:')} {e}")
                    continue

        # Unrecognized input: just redraw the menu
        continue


# ---------------------------------------------------------------------------
# Profile Management
# ---------------------------------------------------------------------------


async def _profile_load_section(cfg: dict) -> dict:
    try:
        from .core.profiles import (
            delete_profile_async,
            get_profile_async,
            list_profiles_async,
        )
    except Exception:
        return cfg

    while True:
        profiles = await list_profiles_async()
        if not profiles:
            return cfg

        _section("Load Profile  (optional)")
        for _i, _name in enumerate(profiles, 1):
            print(f"  {_i}. {_name}")

        print(DIM("\n  [num/name] Select  |  d<num>: Delete profile"))

        try:
            val = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
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
                    {k: v for k, v in profile_data.items() if not k.startswith("_")},
                )
                cfg["_profile_loaded"] = chosen_name
            return cfg


async def _profile_save_section(cfg: dict) -> None:
    if not _ask_bool("Save this configuration as a profile?", False):
        return
    try:
        from .core.profiles import list_profiles_async, save_profile_async
    except Exception:
        return

    existing = await list_profiles_async()
    if existing:
        print(DIM(f"  Existing profiles: {', '.join(existing)}"))

    name = _ask("Profile name", "default").strip()
    if not name:
        return

    with contextlib.suppress(Exception):
        await save_profile_async(name, cfg)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _summary(cfg: dict) -> None:
    _section("Configuration Summary")

    def row(label: str, value: str) -> None:
        pad = " " * max(0, 22 - len(label))
        print(f"  {BOLD(label)}{pad}: {value}")

    row("URL", cfg["url"][:65])
    row("Output Dir", cfg["output_dir"])

    if cfg.get("output_path"):
        row("Exact File Path", cfg["output_path"])
    row("Services", " → ".join(cfg["services"]))
    row("Quality", cfg["quality"])
    if cfg.get("transcode_to"):
        kept = " (original kept)" if cfg.get("transcode_keep_original") else ""
        row(
            "Transcode",
            f"{cfg['transcode_to'].upper()} {cfg.get('transcode_bitrate', '320k')}{kept}",
        )
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

    # Check if URL is a playlist for summary flag
    lower_url = cfg["url"].lower()
    is_playlist = (
        "/playlist/" in lower_url or "list=" in lower_url or "/sets/" in lower_url
    )
    if is_playlist and not cfg.get("create_playlist_subfolders", True):
        flags.append("no-playlist-subfolders")

    if cfg["first_artist_only"]:
        flags.append("first-artist-only")
    if cfg.get("artist_separator"):
        flags.append(f"artist-separator: '{cfg['artist_separator']}'")
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

    concurrent = cfg.get("max_concurrent_downloads", 2)
    row("Parallel downloads", str(concurrent))

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

        if not _ask_bool(
            "Some lyric providers are unreachable. Retry health check?",
            False,
        ):
            break

    cfg: dict = {}

    # ── 1. URL ──────────────────────────────────────────────────────────────
    _section("1 · URL")

    prefill = await _pick_from_history()

    url = ""
    while True:
        if prefill:
            url = _ask("URL", prefill)
            prefill = None
        else:
            url = _ask("URL")

        if not url:
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
            is_blocked = True

        elif "soundcloud.com" in lower_url:
            path = urlparse(url).path.strip("/")
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1 and parts[0] not in ("discover", "stream", "upload"):
                is_blocked = True

        if not is_blocked:
            break

    cfg["url"] = url

    # ── Profile load ────────────────────────────────────────────────────────
    cfg = await _profile_load_section(cfg)

    # ── Extension registries ────────────────────────────────────────────────
    await _manage_registries_section()

    if cfg.get("_profile_loaded"):
        cfg.pop("_profile_loaded", None)
        cfg.setdefault("output_dir", "./Downloads")
        cfg.setdefault("services", ["tidal"])
        cfg.setdefault("filename_format", "{title} - {artist}")
        cfg.setdefault("quality", "LOSSLESS")
        cfg.setdefault("use_track_numbers", False)
        cfg.setdefault("use_album_track_numbers", False)
        cfg.setdefault("use_artist_subfolders", False)
        cfg.setdefault("use_album_subfolders", False)
        cfg.setdefault("first_artist_only", False)
        cfg.setdefault("embed_lyrics", True)
        cfg.setdefault("lyrics_providers", ["apple", "lrclib"])
        cfg.setdefault("enrich_metadata", True)
        cfg.setdefault("enrich_providers", ["deezer", "apple"])
        cfg.setdefault("allow_fallback", True)
        cfg.setdefault("max_concurrent_downloads", 2)
        return cfg

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

        use_custom = _ask_bool(
            "Set a custom output path?",
            bool(cfg.get("output_path")),
        )
        if use_custom:
            cfg["output_path"] = _ask(
                "Full file path including extension",
                cfg.get("output_path", ""),
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
            print(DIM(f"  Unavailable: {', '.join(unavailable)}"))

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
    elif is_youtube_url:
        cfg["services"] = ["youtube"]
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options=["tidal", "qobuz", "deezer", "amazon", "apple", "soundcloud"],
                defaults=["tidal"],
                ordered=True,
            )
            cfg["services"] = ["youtube", *fallbacks]
    elif is_apple_url:
        cfg["services"] = ["apple"]
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options=["tidal", "qobuz", "deezer", "amazon", "youtube"],
                defaults=["tidal"],
                ordered=True,
            )
            cfg["services"] = ["apple", *fallbacks]
    elif is_pandora_url:
        cfg["services"] = ["pandora"]
        add_fallback = _ask_bool("Add fallback providers?", False)
        if add_fallback:
            fallbacks = _ask_multi(
                "Fallback providers (order = priority):",
                options=["tidal", "qobuz", "deezer", "amazon", "apple"],
                defaults=["tidal"],
                ordered=True,
            )
            cfg["services"] = ["pandora", *fallbacks]
    else:
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
    elif is_pandora_url or (
        len(cfg["services"]) == 1 and cfg["services"][0] == "pandora"
    ):
        cfg["allow_fallback"] = True
        quality_default = str(cfg.get("quality", "LOSSLESS") or "LOSSLESS").upper()
        if quality_default not in ["HI_RES_LOSSLESS", "LOSSLESS"]:
            quality_default = "LOSSLESS"
        q_choice = _ask_choice(
            "Pandora Quality:",
            options=[
                "HI_RES_LOSSLESS (Best available; Pandora is lossy)",
                "LOSSLESS (Best available; Pandora is lossy)",
            ],
            default=quality_default,
        )
        cfg["quality"] = normalize_quality(q_choice.split(" ")[0])
    elif is_youtube_url or (
        len(cfg["services"]) == 1 and cfg["services"][0] == "youtube"
    ):
        cfg["quality"] = normalize_quality(
            cfg.get("quality", "LOSSLESS") or "LOSSLESS",
        )
        cfg["allow_fallback"] = True
    else:
        has_qobuz = "qobuz" in cfg["services"]
        has_tidal = "tidal" in cfg["services"]
        has_deezer = "deezer" in cfg["services"]
        has_apple = "apple" in cfg["services"]

        if has_qobuz and not (has_tidal or has_deezer or has_apple):
            q_default = (
                "27 (Hi-Res Max)"
                if normalize_quality(cfg.get("quality", "LOSSLESS"))
                == "HI_RES_LOSSLESS"
                else "6 (CD Lossless)"
            )
            q_choice = _ask_choice(
                "Qobuz Quality:",
                options=["6 (CD Lossless)", "27 (Hi-Res Max)"],
                default=q_default,
            )
            cfg["quality"] = normalize_quality(q_choice.split(" ")[0])
        elif has_tidal and not (has_qobuz or has_deezer or has_apple):
            tidal_default = str(cfg.get("quality", "LOSSLESS") or "LOSSLESS").upper()
            if tidal_default not in ["HI_RES_LOSSLESS", "LOSSLESS"]:
                tidal_default = "LOSSLESS"
            q = _ask_choice(
                "Tidal Quality:",
                options=["HI_RES_LOSSLESS", "LOSSLESS"],
                default=tidal_default,
            )
            cfg["quality"] = normalize_quality(q)
        elif has_deezer and not (has_qobuz or has_tidal or has_apple):
            deezer_default = (
                "HI_RES_LOSSLESS (Best available)"
                if normalize_quality(cfg.get("quality", "LOSSLESS"))
                == "HI_RES_LOSSLESS"
                else "LOSSLESS (FLAC)"
            )
            q_choice = _ask_choice(
                "Deezer Quality:",
                options=["LOSSLESS (FLAC)", "HI_RES_LOSSLESS (Best available)"],
                default=deezer_default,
            )
            cfg["quality"] = normalize_quality(q_choice.split(" ")[0])
        elif has_apple and not (has_qobuz or has_tidal or has_deezer):
            apple_default = (
                "HI_RES_LOSSLESS (Best available)"
                if normalize_quality(cfg.get("quality", "LOSSLESS"))
                == "HI_RES_LOSSLESS"
                else "ALAC (Lossless)"
            )
            q_choice = _ask_choice(
                "Apple Music Quality:",
                options=["ALAC (Lossless)", "HI_RES_LOSSLESS (Best available)"],
                default=apple_default,
            )
            cfg["quality"] = normalize_quality(q_choice.split(" ")[0])
        elif has_qobuz or has_tidal or has_deezer or has_apple:
            combined_options = [
                "LOSSLESS (FLAC on Deezer/Tidal, '6' on Qobuz, ALAC on Apple)",
                "HI_RES_LOSSLESS (Best available everywhere, '27' on Qobuz)",
            ]

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
            else:
                cfg["quality"] = "HI_RES_LOSSLESS"
            # normalize combined choice to canonical form
            cfg["quality"] = normalize_quality(cfg["quality"])
        else:
            q = _ask_choice(
                "Quality:",
                options=["LOSSLESS", "HI_RES_LOSSLESS"],
                default="LOSSLESS",
            )
            cfg["quality"] = normalize_quality(q)

        cfg["allow_fallback"] = _ask_bool(
            "Allow automatic quality fallback?",
            cfg.get("allow_fallback", True),
        )

    # ── 4.5. Transcoding ───────────────────────────────────────────────────
    _section("4.5 · Transcoding")
    if _ask_bool(
        "Convert every track to MP3 after download? (requires ffmpeg)",
        bool(cfg.get("transcode_to")),
    ):
        cfg["transcode_to"] = "mp3"
        cfg["transcode_bitrate"] = _ask_choice(
            "MP3 bitrate:",
            options=["320k", "256k", "192k", "128k"],
            default=cfg.get("transcode_bitrate", "320k"),
        )
        cfg["transcode_keep_original"] = _ask_bool(
            "Keep the original lossless file as well?",
            cfg.get("transcode_keep_original", False),
        )
    else:
        cfg["transcode_to"] = None
        cfg["transcode_bitrate"] = cfg.get("transcode_bitrate", "320k")
        cfg["transcode_keep_original"] = cfg.get("transcode_keep_original", False)

    # ── 5. Filename format ─────────────────────────────────────────────────
    _section("5 · Filename Format")
    cfg["filename_format"] = _ask(
        "Format",
        cfg.get("filename_format", "{title} - {artist}"),
    )

    # ── 6. Organization Options ───────────────────────────────────────────
    _section("6 · Organization Options")

    cfg["use_track_numbers"] = cfg.get("use_track_numbers", False)
    cfg["use_album_track_numbers"] = cfg.get("use_album_track_numbers", False)
    cfg["use_artist_subfolders"] = cfg.get("use_artist_subfolders", False)
    cfg["use_album_subfolders"] = cfg.get("use_album_subfolders", False)
    cfg["create_playlist_subfolders"] = cfg.get("create_playlist_subfolders", True)
    cfg["first_artist_only"] = cfg.get("first_artist_only", False)

    # Check if URL is a playlist for organization question
    lower_url = cfg["url"].lower()
    is_playlist = (
        "/playlist/" in lower_url or "list=" in lower_url or "/sets/" in lower_url
    )

    if is_playlist:
        cfg["create_playlist_subfolders"] = _ask_bool(
            "Create subfolders for playlists?",
            cfg["create_playlist_subfolders"],
        )
    else:
        cfg["create_playlist_subfolders"] = (
            True  # Mantieni il default se non è una playlist
        )

    cfg["use_track_numbers"] = _ask_bool(
        "Add track number to filename?",
        cfg["use_track_numbers"],
    )

    if cfg["use_track_numbers"]:
        cfg["use_album_track_numbers"] = _ask_bool(
            "Use original album track number?",
            cfg["use_album_track_numbers"],
        )
        cfg["use_artist_subfolders"] = False
        cfg["use_album_subfolders"] = False
        cfg["first_artist_only"] = False
    else:
        cfg["use_album_track_numbers"] = False
        cfg["use_artist_subfolders"] = _ask_bool(
            "Create artist subfolders?",
            cfg["use_artist_subfolders"],
        )
        cfg["use_album_subfolders"] = _ask_bool(
            "Create album subfolders?",
            cfg["use_album_subfolders"],
        )
        cfg["first_artist_only"] = _ask_bool(
            "Use only the first artist in tags and filename?",
            cfg["first_artist_only"],
        )

    if not cfg["first_artist_only"]:
        sep = _ask(
            "Artist separator (leave blank for standard multi-value tags, e.g. ', ' or ' / ')",
            cfg.get("artist_separator") or "",
        )
        cfg["artist_separator"] = sep if sep else None
    else:
        cfg["artist_separator"] = None

    # ── 7. Lyrics ────────────────────────────────────────────────────────────
    _section("7 · Lyrics")
    cfg["embed_lyrics"] = _ask_bool(
        "Embed synchronized lyrics?",
        cfg.get("embed_lyrics", True),
    )

    if cfg["embed_lyrics"]:
        cfg["lyrics_providers"] = _ask_multi(
            "Lyrics providers (order = priority):",
            options=[
                "spotify",
                "apple",
                "deezer",
                "genius",
                "netease",
                "qq",
                "youtube",
                "kugou",
                "musixmatch",
                "lrclib",
                "amazon",
            ],
            defaults=cfg.get("lyrics_providers") or ["apple", "lrclib"],
            ordered=True,
        )
    else:
        cfg["lyrics_providers"] = cfg.get("lyrics_providers") or [
            "apple",
            "lrclib",
            "amazon",
        ]

    # ── 8. Metadata enrichment ──────────────────────────────────────────────
    _section("8 · Metadata Enrichment")
    cfg["enrich_metadata"] = _ask_bool(
        "Enable metadata enrichment?",
        cfg.get("enrich_metadata", True),
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
    default_retries = cfg.get("track_max_retries", 0)
    retry_str = _ask("Extra retries per track (0 = no retry)", str(default_retries))
    try:
        cfg["track_max_retries"] = max(0, int(retry_str))
    except ValueError:
        cfg["track_max_retries"] = 0

    # ── 9.3. Concurrency ─────────────────────────────────────────────────
    _section("9.3 · Concurrency")
    default_concurrent = cfg.get("max_concurrent_downloads", 2)
    concurrent_str = _ask(
        "Tracks to download in parallel (1 = sequential, no interleaved output)",
        str(default_concurrent),
    )
    try:
        cfg["max_concurrent_downloads"] = max(1, int(concurrent_str))
    except ValueError:
        cfg["max_concurrent_downloads"] = default_concurrent

    # ── 9.5. Timeout ───────────────────────────────────────────────────
    _section("9.5 · Download Timeout")
    default_timeout = cfg.get("timeout_s", 0)
    timeout_str = _ask(
        "Timeout per track in seconds (0 = disabled)",
        str(default_timeout),
    )
    try:
        cfg["timeout_s"] = max(0, int(timeout_str))
    except ValueError:
        cfg["timeout_s"] = 0

    # ── 10. Post-download Action ─────────────────────────────────────────────
    _section("10 · Post-Download Action")

    action_options = ["none", "open_folder", "notify", "command"]
    default_action = cfg.get("post_download_action", "none")
    action_choice = _ask_choice(
        "Action on completion:",
        options=action_options,
        default=default_action,
    )
    cfg["post_download_action"] = action_choice

    if action_choice == "command":
        cfg["post_download_command"] = _ask(
            "Shell command",
            cfg.get(
                "post_download_command",
                "echo 'Done: {succeeded} tracks in {folder}'",
            ),
        )
    else:
        cfg["post_download_command"] = cfg.get("post_download_command", "")

    # ── 11. Optional Qobuz Local API ───────────────────────────────────────────────
    _section("11 · Optional Qobuz Local API")
    cfg["qobuz_local_api_url"] = (
        _ask(
            "Qobuz local API URL (leave blank to skip)",
            cfg.get("qobuz_local_api_url", "") or "",
        )
        or None
    )

    # ── 11.5. Custom Tidal API ───────────────────────────────────────────────
    cfg["tidal_custom_api"] = (
        _ask(
            "Custom Tidal API URL (leave blank to skip)",
            cfg.get("tidal_custom_api", "") or "",
        )
        or None
    )

    # ── 12. Loop ─────────────────────────────────────────────────────────────
    loop_str = _ask(
        "Repeat every N minutes (leave blank to disable)",
        str(cfg.get("loop", "")),
    )
    cfg["loop"] = int(loop_str) if loop_str.isdigit() else None

    # ── Profile save ────────────────────────────────────────────────────────
    await _profile_save_section(cfg)

    # ── Summary + confirmation ───────────────────────────────────────────────
    _summary(cfg)
    if not _ask_bool(BOLD("Start download with this configuration?"), True):
        sys.exit(0)

    _section("Equivalent CLI command")
    _print_cli_command(cfg)

    return cfg


def _print_cli_command(cfg: dict) -> None:
    """Display the equivalent command-line invocation for a configuration.

    Parameters
    ----------
        cfg (dict): Configuration values used to construct the command.

    """
    parts = [f'spotiflac "{cfg["url"]}" "{cfg["output_dir"]}"']
    if cfg.get("output_path"):
        parts.append(f'-o "{cfg["output_path"]}"')
    parts.append(f"-s {' '.join(cfg['services'])}")
    if cfg["quality"] not in ("LOSSLESS", "BEST"):
        parts.append(f"-q {cfg['quality']}")
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

    # Check if URL is a playlist before appending the CLI flag
    lower_url = cfg["url"].lower()
    is_playlist = (
        "/playlist/" in lower_url or "list=" in lower_url or "/sets/" in lower_url
    )
    if is_playlist and not cfg.get("create_playlist_subfolders", True):
        parts.append("--no-playlist-subfolders")

    if cfg["first_artist_only"]:
        parts.append("--first-artist-only")
    if cfg.get("artist_separator"):
        parts.append(f'--artist-separator {shlex.quote(cfg["artist_separator"])}')
    if not cfg["embed_lyrics"]:
        parts.append("--no-lyrics")
    else:
        parts.append(f"--lyrics-providers {' '.join(cfg['lyrics_providers'])}")
    if not cfg["enrich_metadata"]:
        parts.append("--no-enrich")
    else:
        parts.append(f"--enrich-providers {' '.join(cfg['enrich_providers'])}")
    if cfg.get("track_max_retries"):
        parts.append(f"--retries {cfg['track_max_retries']}")
    if cfg.get("max_concurrent_downloads", 2) != 2:
        parts.append(f"--max-concurrent {cfg['max_concurrent_downloads']}")
    if cfg.get("timeout_s"):
        parts.append(f"--timeout {cfg['timeout_s']}")
    if cfg.get("post_download_action") and cfg["post_download_action"] != "none":
        parts.append(f"--post-action {cfg['post_download_action']}")
        if cfg["post_download_action"] == "command" and cfg.get(
            "post_download_command",
        ):
            parts.append(f'--post-command "{cfg["post_download_command"]}"')
    if cfg.get("qobuz_local_api_url"):
        parts.append(f'--qobuz-local-api "{cfg["qobuz_local_api_url"]}"')
    if cfg.get("tidal_custom_api"):
        parts.append(f'--tidal-api "{cfg["tidal_custom_api"]}"')
    if cfg.get("loop"):
        parts.append(f"--loop {cfg['loop']}")

    command = " \\\n    ".join(parts)
    print(f"\n  {command}\n")

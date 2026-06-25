#!/usr/bin/env python3
"""
CLI entry point for SpotiFLAC.

=== Migrazione async ===
L'intero entry point ora gira su un singolo event loop condiviso, invece di
delegare a `SpotiFLAC(...)` (wrapper sincrono che apre un proprio
asyncio.run() al suo interno). Si usa direttamente `SpotiflacDownloader`,
che è già 100% async-native, ed è per questo che `check_for_updates_async`
e `run_interactive()` possono ora essere "await"-ati nello stesso loop
invece di aprirne uno nuovo ciascuno.

New flags vs previous version:
  --retries N               Extra download attempts per track (default: 0)
  --post-action ACTION      Action after all downloads finish (none|open_folder|notify|command)
  --post-command CMD        Shell command to run when --post-action=command
  --profile NAME            Load a saved profile before parsing remaining args
  --save-profile NAME       Save current args as a named profile after run
  --timeout SECONDS         Max seconds per track download; track skipped if exceeded
"""
import argparse
import logging
import sys
import json
import os
import asyncio

from .check_update import check_for_updates_async
from .downloader import SpotiflacDownloader, DownloadOptions
from .interactive import run_interactive


def load_config() -> dict:
    config_path = "config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            from .core.profiles import ProfileConfig
            return ProfileConfig.model_validate(raw).model_dump(exclude_none=True)
        except json.JSONDecodeError as e:
            print(f"Error loading config.json: invalid JSON: {e}")
        except Exception as e:
            print(f"Error loading config.json: {e}")
    return {}


def _load_profile_into_defaults(profile_name: str) -> dict:
    """Return profile data dict, or empty dict on failure."""
    try:
        from .core.profiles import get_profile
        data = get_profile(profile_name)
        if data:
            print(f"[profile] Loaded: {profile_name}")
            return data
        print(f"[profile] Not found: {profile_name}")
    except Exception as exc:
        print(f"[profile] Load error: {exc}")
    return {}


def parse_args(profile_defaults: dict | None = None) -> argparse.Namespace:
    pd = profile_defaults or {}

    parser = argparse.ArgumentParser(
        prog            = "spotiflac",
        description     = "Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube, Pandora and more.",
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("url",        nargs="?", help="Spotify, Tidal, Apple Music, SoundCloud, YouTube or Pandora URL")
    parser.add_argument("output_dir", nargs="?", help="Destination directory")

    parser.add_argument(
        "--service", "-s",
        choices = [
            "deezer", "tidal", "qobuz", "amazon", "joox", "netease",
            "migu", "kuwo", "soundcloud", "youtube", "apple", "pandora"
        ],
        nargs   = "+",
        default = pd.get("services", ["tidal"]),
        metavar = "SERVICE",
        help    = "Audio providers in priority order (default: tidal). "
                  "Choices: tidal, qobuz, deezer, amazon, joox, netease, migu, kuwo, soundcloud, youtube, apple, pandora",
    )
    parser.add_argument(
        "--filename-format", "-f",
        default = pd.get("filename_format", "{title} - {artist}"),
        dest    = "filename_format",
        help    = "Filename template with placeholders",
    )
    parser.add_argument(
        "--output-path", "-o",
        default = pd.get("output_path", None),
        dest    = "output_path",
        metavar = "FILE",
        help    = "Exact output file path for single track downloads",
    )
    parser.add_argument(
        "--quality", "-q",
        default = pd.get("quality", "LOSSLESS"),
        help = "Quality: DOLBY_ATMOS, HI_RES_LOSSLESS, LOSSLESS, HIGH, LOW (Tidal). "
               "Qobuz: 27, 7, 6. Apple: alac, atmos, ac3, aac. "
               "Pandora: mp3_192, aac_64, aac_32. Default: LOSSLESS"
    )
    parser.add_argument("--use-track-numbers",       action="store_true", dest="use_track_numbers",       default=pd.get("use_track_numbers", False))
    parser.add_argument("--use-album-track-numbers", action="store_true", dest="use_album_track_numbers", default=pd.get("use_album_track_numbers", False))
    parser.add_argument("--use-artist-subfolders",   action="store_true", dest="use_artist_subfolders",   default=pd.get("use_artist_subfolders", False))
    parser.add_argument("--use-album-subfolders",    action="store_true", dest="use_album_subfolders",    default=pd.get("use_album_subfolders", False))
    parser.add_argument("--first-artist-only",       action="store_true", dest="first_artist_only",       default=pd.get("first_artist_only", False))
    parser.add_argument("--qobuz-local-api", default=pd.get("qobuz_local_api_url", None), dest="qobuz_local_api_url", metavar="URL")
    parser.add_argument(
        "--tidal-api",
        default = pd.get("tidal_custom_api", None),
        dest    = "tidal_custom_api",
        metavar = "URL",
        help    = "URL of a self-hosted hifi-api instance (https://github.com/binimum/hifi-api). "
                  "Takes priority over built-in API pool.",
    )
    parser.add_argument("--loop", "-l", type=int, default=pd.get("loop", None))
    parser.add_argument("--verbose", "-v", action="store_true", default=pd.get("verbose", False))
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=pd.get("interactive", False),
        help="Launch interactive mode (wizard)"
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        default=False,
        help="Launch graphical user interface (GUI)"
    )

    # ── Profile ─────────────────────────────────────────────────────────────
    profile_grp = parser.add_argument_group("Profile")
    profile_grp.add_argument(
        "--profile",
        default = None,
        metavar = "NAME",
        help    = "Load a saved profile (overrides config.json defaults, CLI flags take precedence)",
    )
    profile_grp.add_argument(
        "--save-profile",
        default = None,
        dest    = "save_profile",
        metavar = "NAME",
        help    = "Save the current configuration as a named profile after the run",
    )

    # ── Lyrics ──────────────────────────────────────────────────────────────
    lyrics_grp = parser.add_argument_group("Lyrics")
    lyrics_grp.add_argument(
        "--no-lyrics",
        action  = "store_false",
        dest    = "embed_lyrics",
        help    = "Disable lyrics embedding (enabled by default)",
    )
    parser.set_defaults(embed_lyrics=pd.get("embed_lyrics", True))
    lyrics_grp.add_argument(
        "--lyrics-providers",
        nargs   = "+",
        default = pd.get("lyrics_providers", ["spotify", "apple", "lrclib", "amazon"]),
        dest    = "lyrics_providers",
        choices = ["spotify", "apple", "musixmatch", "amazon", "lrclib"],
    )

    # ── Metadata enrichment ─────────────────────────────────────────────────
    enrich_grp = parser.add_argument_group("Metadata Enrichment")
    enrich_grp.add_argument(
        "--no-enrich",
        action  = "store_false",
        dest    = "enrich",
        help    = "Disable metadata enrichment (enabled by default)",
    )
    parser.set_defaults(enrich=pd.get("enrich_metadata", True))
    enrich_grp.add_argument(
        "--enrich-providers",
        nargs   = "+",
        default = pd.get("enrich_providers", ["deezer", "apple", "qobuz", "tidal", "soundcloud"]),
        dest    = "enrich_providers",
        choices = ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
    )

    # ── Retry ────────────────────────────────────────────────────────────────
    retry_grp = parser.add_argument_group("Retry")
    retry_grp.add_argument(
        "--retries",
        type    = int,
        default = pd.get("track_max_retries", 0),
        dest    = "retries",
        metavar = "N",
        help    = "Extra download attempts per track on failure (default: 0). "
                  "Retries cycle through all providers with exponential backoff (2s, 4s, 8s…).",
    )

    # ── Timeout ──────────────────────────────────────────────────────────────
    timeout_grp = parser.add_argument_group("Timeout")
    timeout_grp.add_argument(
        "--timeout",
        type    = int,
        default = pd.get("timeout_s", None),
        dest    = "timeout_s",
        metavar = "SECONDS",
        help    = "Maximum seconds allowed per track download (default: no limit). "
                  "The track is skipped and counted as failed when the timeout expires.",
    )

    # ── Post-download ─────────────────────────────────────────────────────────
    post_grp = parser.add_argument_group("Post-Download")
    post_grp.add_argument(
        "--post-action",
        choices = ["none", "open_folder", "notify", "command"],
        default = pd.get("post_download_action", "none"),
        dest    = "post_action",
        help    = "Action to perform after all downloads finish (default: none)",
    )
    post_grp.add_argument(
        "--post-command",
        default = pd.get("post_download_command", ""),
        dest    = "post_command",
        metavar = "CMD",
        help    = "Shell command for --post-action=command. "
                  "Placeholders: {folder} {succeeded} {failed}",
    )

    return parser.parse_args()


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
    loop: int | None,
    quality: str,
    first_artist_only: bool,
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
) -> None:
    """
    Bridge async verso SpotiflacDownloader, senza passare per il wrapper
    sincrono `SpotiFLAC()` (che farebbe un `asyncio.run()` annidato e
    fallirebbe perché siamo già dentro un loop).
    """
    logger = logging.getLogger("SpotiFLAC")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(log_level)

    opts = DownloadOptions(
        output_dir              = output_dir,
        services                = services,
        filename_format         = filename_format,
        use_track_numbers       = use_track_numbers,
        use_album_track_numbers = use_album_track_numbers,
        use_artist_subfolders   = use_artist_subfolders,
        allow_fallback          = allow_fallback,
        use_album_subfolders    = use_album_subfolders,
        quality                 = quality,
        first_artist_only       = first_artist_only,
        output_path             = output_path,
        embed_lyrics            = embed_lyrics,
        lyrics_providers        = lyrics_providers,
        enrich_metadata         = enrich_metadata,
        enrich_providers        = enrich_providers,
        qobuz_local_api_url     = qobuz_local_api_url,
        track_max_retries       = track_max_retries,
        post_download_action    = post_download_action,
        post_download_command   = post_download_command,
        tidal_custom_api        = tidal_custom_api,
        timeout_s               = timeout_s,
    )

    try:
        downloader = SpotiflacDownloader(opts)
        await downloader.run_async(url, loop_minutes=loop)
    except KeyboardInterrupt:
        print("\n\n[!] Operazione interrotta dall'utente.")
    except Exception as e:
        logging.getLogger("SpotiFLAC").error("Critical error durante l'esecuzione: %s", e)


async def amain() -> None:
    """Entry point async-nativo. Tutto il flusso CLI vive su un solo event loop."""
    from .core.ffmpeg_check import print_ffmpeg_warning

    # Controllo aggiornamenti non bloccante: non deve mai impedire l'avvio.
    try:
        await check_for_updates_async()
    except Exception:
        pass

    # GUI mode (explicit --gui flag) — resta sincrona: la GUI gestisce il proprio loop.
    if "--gui" in sys.argv:
        from .app import run_gui
        run_gui()
        return

    # Interactive mode (explicit --interactive flag)
    if "--interactive" in sys.argv:
        print_ffmpeg_warning()
        cfg = await run_interactive()

        log_level = logging.WARNING
        logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        await _run_download_async(
            cfg["url"],
            output_dir               = cfg["output_dir"],
            services                 = cfg["services"],
            filename_format          = cfg["filename_format"],
            use_track_numbers        = cfg["use_track_numbers"],
            use_album_track_numbers  = cfg["use_album_track_numbers"],
            use_artist_subfolders    = cfg["use_artist_subfolders"],
            use_album_subfolders     = cfg["use_album_subfolders"],
            loop                     = cfg.get("loop"),
            quality                  = cfg["quality"],
            first_artist_only        = cfg["first_artist_only"],
            log_level                = log_level,
            output_path              = cfg.get("output_path"),
            allow_fallback           = cfg.get("allow_fallback", True),
            embed_lyrics             = cfg["embed_lyrics"],
            lyrics_providers         = cfg["lyrics_providers"],
            enrich_metadata          = cfg["enrich_metadata"],
            enrich_providers         = cfg["enrich_providers"],
            qobuz_local_api_url      = cfg.get("qobuz_local_api_url"),
            tidal_custom_api         = cfg.get("tidal_custom_api") or None,
            track_max_retries        = cfg.get("track_max_retries", 0),
            post_download_action     = cfg.get("post_download_action", "none"),
            post_download_command    = cfg.get("post_download_command", ""),
            timeout_s                = cfg.get("timeout_s"),
        )
        return

    if len(sys.argv) == 1:
        parser = argparse.ArgumentParser(
            prog            = "spotiflac",
            description     = "Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube, Pandora and more.",
            formatter_class = argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("--gui", action="store_true", help="Launch graphical user interface (GUI)")
        parser.add_argument("--interactive", action="store_true", help="Launch interactive mode (wizard)")
        parser.print_help()
        return

    # ── CLI mode ──────────────────────────────────────────────────────
    print_ffmpeg_warning()
    profile_defaults: dict = {}
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        if idx + 1 < len(sys.argv):
            profile_defaults = _load_profile_into_defaults(sys.argv[idx + 1])

    file_cfg = load_config()
    merged_defaults = {**file_cfg, **profile_defaults}

    args = parse_args(profile_defaults=merged_defaults)

    # Check that URL and output_dir are provided for CLI mode
    if not args.url or not args.output_dir:
        parser = argparse.ArgumentParser(
            prog            = "spotiflac",
            description     = "Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube, Pandora and more.",
            formatter_class = argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("--gui", action="store_true", help="Launch graphical user interface (GUI)")
        parser.add_argument("--interactive", action="store_true", help="Launch interactive mode (wizard)")
        parser.print_help()
        return

    quality             = args.quality or merged_defaults.get("quality", "LOSSLESS")
    qobuz_local_api_url = args.qobuz_local_api_url or merged_defaults.get("qobuz_local_api_url")
    tidal_custom_api    = args.tidal_custom_api or merged_defaults.get("tidal_custom_api")
    timeout_s           = args.timeout_s if args.timeout_s is not None else merged_defaults.get("timeout_s")
    track_max_retries   = args.retries if args.retries is not None else merged_defaults.get("track_max_retries", 0)

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    log_format = "%(levelname)s:%(name)s: %(message)s" if args.verbose else "%(levelname)s: %(message)s"
    logging.basicConfig(level=log_level, format=log_format)

    await _run_download_async(
        args.url,
        output_dir               = args.output_dir,
        services                 = args.service,
        filename_format          = args.filename_format,
        use_track_numbers        = args.use_track_numbers,
        use_album_track_numbers  = args.use_album_track_numbers,
        use_artist_subfolders    = args.use_artist_subfolders,
        use_album_subfolders     = args.use_album_subfolders,
        loop                     = args.loop,
        quality                  = quality,
        first_artist_only        = args.first_artist_only,
        log_level                = log_level,
        output_path              = args.output_path,
        allow_fallback           = True,
        embed_lyrics             = args.embed_lyrics,
        lyrics_providers         = args.lyrics_providers,
        enrich_metadata          = args.enrich,
        enrich_providers         = args.enrich_providers,
        qobuz_local_api_url      = qobuz_local_api_url,
        tidal_custom_api         = tidal_custom_api,
        track_max_retries        = track_max_retries,
        post_download_action     = args.post_action,
        post_download_command    = args.post_command,
        timeout_s                = timeout_s,
    )

    if args.save_profile:
        try:
            from .core.profiles import save_profile
            profile_cfg = {
                "services":              args.service,
                "quality":               quality,
                "filename_format":       args.filename_format,
                "use_track_numbers":     args.use_track_numbers,
                "use_album_track_numbers": args.use_album_track_numbers,
                "use_artist_subfolders": args.use_artist_subfolders,
                "use_album_subfolders":  args.use_album_subfolders,
                "first_artist_only":     args.first_artist_only,
                "allow_fallback":        True,
                "embed_lyrics":          args.embed_lyrics,
                "lyrics_providers":      args.lyrics_providers,
                "enrich_metadata":       args.enrich,
                "enrich_providers":      args.enrich_providers,
                "track_max_retries":     track_max_retries,
                "post_download_action":  args.post_action,
                "post_download_command": args.post_command,
                "qobuz_local_api_url":   qobuz_local_api_url,
                "tidal_custom_api":      tidal_custom_api,
                "timeout_s":             timeout_s,
                "loop":                  args.loop,
            }
            save_profile(args.save_profile, profile_cfg)
            print(f"[profile] Saved as: {args.save_profile}")
        except Exception as exc:
            print(f"[profile] Save error: {exc}")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
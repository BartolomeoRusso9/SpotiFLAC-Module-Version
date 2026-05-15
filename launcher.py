#!/usr/bin/env python3
"""
CLI entry point for SpotiFLAC.
New flags vs previous version:
  --retries N               Extra download attempts per track (default: 0)
  --post-action ACTION      Action after all downloads finish (none|open_folder|notify|command)
  --post-command CMD        Shell command to run when --post-action=command
  --profile NAME            Load a saved profile before parsing remaining args
  --save-profile NAME       Save current args as a named profile after run
"""
import argparse
import logging
import sys
import json
import os

from SpotiFLAC.check_update import check_for_updates
from SpotiFLAC import SpotiFLAC
from SpotiFLAC.interactive import run_interactive


def load_config() -> dict:
    config_path = "config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config.json: {e}")
    return {}


def _load_profile_into_defaults(profile_name: str) -> dict:
    """Return profile data dict, or empty dict on failure."""
    try:
        from SpotiFLAC.core.profiles import get_profile
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
        description     = "Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube and more.",
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("url",        help="Spotify, Tidal, Apple Music, SoundCloud or YouTube URL")
    parser.add_argument("output_dir", help="Destination directory")

    parser.add_argument(
        "--service", "-s",
        choices = ["deezer", "tidal", "qobuz", "amazon", "spoti", "soundcloud", "youtube", "apple"],
        nargs   = "+",
        default = pd.get("services", ["tidal"]),
        metavar = "SERVICE",
        help    = "Audio providers in priority order (default: tidal)",
    )
    parser.add_argument(
        "--filename-format", "-f",
        default = pd.get("filename_format", "{title} - {artist}"),
        dest    = "filename_format",
        help    = "Filename template with placeholders",
    )
    parser.add_argument(
        "--output-path", "-o",
        default = None,
        dest    = "output_path",
        metavar = "FILE",
        help    = "Exact output file path for single track downloads",
    )
    parser.add_argument(
        "--quality", "-q",
        default = pd.get("quality", "LOSSLESS"),
        help    = "Quality: LOSSLESS, HI_RES, HIGH, NORMAL. Default: LOSSLESS",
    )
    parser.add_argument("--use-track-numbers",       action="store_true", dest="use_track_numbers",       default=pd.get("use_track_numbers", False))
    parser.add_argument("--use-album-track-numbers", action="store_true", dest="use_album_track_numbers", default=pd.get("use_album_track_numbers", False))
    parser.add_argument("--use-artist-subfolders",   action="store_true", dest="use_artist_subfolders",   default=pd.get("use_artist_subfolders", False))
    parser.add_argument("--use-album-subfolders",    action="store_true", dest="use_album_subfolders",    default=pd.get("use_album_subfolders", False))
    parser.add_argument("--first-artist-only",       action="store_true", dest="first_artist_only",       default=pd.get("first_artist_only", False))
    parser.add_argument(
        "--include-featuring",
        action  = "store_true",
        dest    = "include_featuring",
        default = pd.get("include_featuring", False),
    )
    parser.add_argument("--qobuz-token", default=None, dest="qobuz_token")
    parser.add_argument("--loop", "-l", type=int, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")

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


def main() -> None:
    check_for_updates()

    if len(sys.argv) == 1:
        # ── Interactive wizard ─────────────────────────────────────────────
        cfg = run_interactive()

        log_level = logging.WARNING
        logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        SpotiFLAC(
            url                      = cfg["url"],
            output_dir               = cfg["output_dir"],
            services                 = cfg["services"],
            filename_format           = cfg["filename_format"],
            use_track_numbers        = cfg["use_track_numbers"],
            use_album_track_numbers  = cfg["use_album_track_numbers"],
            use_artist_subfolders    = cfg["use_artist_subfolders"],
            use_album_subfolders     = cfg["use_album_subfolders"],
            loop                     = cfg.get("loop"),
            quality                  = cfg["quality"],
            first_artist_only         = cfg["first_artist_only"],
            log_level                = log_level,
            output_path              = cfg.get("output_path"),
            allow_fallback           = cfg.get("allow_fallback", True),
            embed_lyrics             = cfg["embed_lyrics"],
            lyrics_providers         = cfg["lyrics_providers"],
            enrich_metadata          = cfg["enrich_metadata"],
            enrich_providers         = cfg["enrich_providers"],
            qobuz_token              = cfg.get("qobuz_token"),
            include_featuring        = cfg["include_featuring"],
            track_max_retries        = cfg.get("track_max_retries", 0),
            post_download_action     = cfg.get("post_download_action", "none"),
            post_download_command    = cfg.get("post_download_command", ""),
        )

    else:
        # ── CLI mode ──────────────────────────────────────────────────────
        # Pre-parse to detect --profile before building full defaults
        profile_defaults: dict = {}
        if "--profile" in sys.argv:
            idx = sys.argv.index("--profile")
            if idx + 1 < len(sys.argv):
                profile_defaults = _load_profile_into_defaults(sys.argv[idx + 1])

        file_cfg = load_config()
        # config.json < profile < CLI flags  (each layer overrides the previous)
        merged_defaults = {**file_cfg, **profile_defaults}

        args = parse_args(profile_defaults=merged_defaults)

        quality     = args.quality     or merged_defaults.get("quality", "LOSSLESS")
        qobuz_token = args.qobuz_token or merged_defaults.get("qobuz_token")

        log_level = logging.DEBUG if args.verbose else logging.WARNING
        logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        SpotiFLAC(
            url                      = args.url,
            output_dir               = args.output_dir,
            services                 = args.service,
            filename_format           = args.filename_format,
            use_track_numbers        = args.use_track_numbers,
            use_album_track_numbers  = args.use_album_track_numbers,
            use_artist_subfolders    = args.use_artist_subfolders,
            use_album_subfolders     = args.use_album_subfolders,
            loop                     = args.loop,
            quality                  = quality,
            first_artist_only         = args.first_artist_only,
            log_level                = log_level,
            output_path              = args.output_path,
            embed_lyrics             = args.embed_lyrics,
            lyrics_providers         = args.lyrics_providers,
            enrich_metadata          = args.enrich,
            enrich_providers         = args.enrich_providers,
            qobuz_token              = qobuz_token,
            include_featuring        = args.include_featuring,
            track_max_retries        = args.retries,
            post_download_action     = args.post_action,
            post_download_command    = args.post_command,
        )

        # Save profile after run if requested
        if args.save_profile:
            try:
                from SpotiFLAC.core.profiles import save_profile
                profile_cfg = {
                    "services":              args.service,
                    "quality":               quality,
                    "filename_format":        args.filename_format,
                    "use_track_numbers":     args.use_track_numbers,
                    "use_album_track_numbers": args.use_album_track_numbers,
                    "use_artist_subfolders": args.use_artist_subfolders,
                    "use_album_subfolders":  args.use_album_subfolders,
                    "first_artist_only":      args.first_artist_only,
                    "allow_fallback":        True,
                    "embed_lyrics":          args.embed_lyrics,
                    "lyrics_providers":      args.lyrics_providers,
                    "enrich_metadata":       args.enrich,
                    "enrich_providers":      args.enrich_providers,
                    "include_featuring":     args.include_featuring,
                    "track_max_retries":     args.retries,
                    "post_download_action":  args.post_action,
                    "post_download_command": args.post_command,
                }
                save_profile(args.save_profile, profile_cfg)
                print(f"[profile] Saved as: {args.save_profile}")
            except Exception as exc:
                print(f"[profile] Save error: {exc}")


if __name__ == "__main__":
    main()
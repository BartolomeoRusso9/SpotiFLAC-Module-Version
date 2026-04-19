#!/usr/bin/env python3
"""
CLI entry point per SpotiFLAC.

Esempio:
    python launcher.py "https://open.spotify.com/album/..." ./Music \
        --service qobuz tidal spoti \
        --filename-format "{year} - {album}/{track}. {title}" \
        --use-artist-subfolders --use-album-subfolders \
        --loop 60
"""
import argparse
import logging
import sys

from SpotiFLAC import SpotiFLAC


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog        = "spotiflac",
        description = "Download Spotify tracks in true FLAC via Tidal, Qobuz, and more.",
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("url",        help="Spotify URL (track, album, or playlist)")
    parser.add_argument("output_dir", help="Destination directory")

    parser.add_argument(
        "--service", "-s",
        choices = ["tidal", "qobuz", "spoti", "deezer", "amazon", "youtube"],
        nargs   = "+",
        default = ["tidal"],
        metavar = "SERVICE",
        help    = "Provider priority list (default: tidal)",
    )
    parser.add_argument(
        "--filename-format", "-f",
        default = "{title} - {artist}",
        dest    = "filename_format",
        help    = "Filename template. Placeholders: {title} {artist} {album} "
                  "{album_artist} {year} {date} {track} {disc} {isrc}",
    )
    parser.add_argument(
        "--quality", "-q",
        default = "LOSSLESS",
        help    = "Quality: LOSSLESS or HI_RES (Tidal), 6/7/27 (Qobuz). Default: LOSSLESS",
    )
    parser.add_argument("--use-track-numbers",     action="store_true", dest="use_track_numbers")
    parser.add_argument("--use-artist-subfolders", action="store_true", dest="use_artist_subfolders")
    parser.add_argument("--use-album-subfolders",  action="store_true", dest="use_album_subfolders")
    parser.add_argument("--first-artist-only",     action="store_true", dest="first_artist_only",
                        help="Use only the first artist in tags and filename")
    parser.add_argument(
        "--loop", "-l",
        type    = int,
        default = None,
        metavar = "MINUTES",
        help    = "Re-run every N minutes (default: single run)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action  = "store_true",
        help    = "Enable verbose logging (DEBUG level)",
    )

    return parser.parse_args()


def main() -> None:
    args      = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.WARNING

    SpotiFLAC(
        url                   = args.url,
        output_dir            = args.output_dir,
        services              = args.service,
        filename_format       = args.filename_format,
        use_track_numbers     = args.use_track_numbers,
        use_artist_subfolders = args.use_artist_subfolders,
        use_album_subfolders  = args.use_album_subfolders,
        loop                  = args.loop,
        quality               = args.quality,
        first_artist_only     = args.first_artist_only,
        log_level             = log_level,
    )


if __name__ == "__main__":
    main()

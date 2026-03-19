"""
Launcher script for SpotiFLAC CLI
"""
import argparse
import sys
import os

if getattr(sys, 'frozen', False):
    application_path = sys._MEIPASS
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, application_path)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="Spotify URL")
    parser.add_argument("output_dir", help="Output directory")
    parser.add_argument(
        "--service",
        choices=["tidal", "qobuz", "amazon", "youtube"],
        nargs="+",
        default=["tidal", "qobuz", "amazon"],
        help="One or more services to try in order",
    )
    parser.add_argument(
        "--filename-format",
        default="{title} - {artist}",
        help="Custom filename format using placeholders (see examples below)"
    )
    parser.add_argument("--use-track-numbers", action="store_true", help="(Deprecated - use {track} in format)")
    parser.add_argument("--use-artist-subfolders", action="store_true")
    parser.add_argument("--use-album-subfolders", action="store_true")
    parser.add_argument("--loop", type=int, help="Loop delay in minutes")
    return parser.parse_args()

def main():
    from SpotiFLAC.SpotiFLAC import SpotiFLAC

    args = parse_args()
    SpotiFLAC(
        args.url, 
        args.output_dir, 
        args.service, 
        args.filename_format, 
        args.use_track_numbers, 
        args.use_artist_subfolders, 
        args.use_album_subfolders, 
        args.loop
    )

if __name__ == '__main__':
    main()
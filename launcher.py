#!/usr/bin/env python3
"""
CLI entry point per SpotiFLAC — con supporto provider lyrics e metadata enrichment ATTIVI di default.
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
            print(f"Errore nel caricamento di config.json: {e}")
    return {}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog            = "spotiflac",
        description     = "Download tracks in true FLAC/MP3 via Deezer, Tidal, Qobuz, SoundCloud, YouTube e altri.",
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("url",        help="URL Spotify, Tidal, SoundCloud o YouTube (track, album, playlist, artist)")
    parser.add_argument("output_dir", help="Directory di destinazione")

    parser.add_argument(
        "--service", "-s",
        choices = ["deezer", "tidal", "qobuz", "amazon", "spoti", "soundcloud", "youtube", "apple"],
        nargs   = "+",
        default = ["tidal"],
        metavar = "SERVICE",
        help    = "Provider audio in ordine di priorità (default: tidal)",
    )
    parser.add_argument(
        "--filename-format", "-f",
        default = "{title} - {artist}",
        dest    = "filename_format",
        help    = "Template filename. Placeholder: {title} {artist} {album} "
                  "{album_artist} {year} {date} {track} {disc} {isrc} {position}",
    )
    parser.add_argument(
        "--output-path", "-o",
        default = None,
        dest    = "output_path",
        metavar = "FILE",
        help    = "Percorso esatto del file di output per tracce singole "
                  "(es. files/song.flac). Sovrascrive output_dir + filename_format.",
    )
    parser.add_argument(
        "--quality", "-q",
        default = "LOSSLESS",
        help    = "Quality: LOSSLESS, HI_RES, HIGH, NORMAL. Default: LOSSLESS",
    )
    parser.add_argument("--use-track-numbers",       action="store_true", dest="use_track_numbers")
    parser.add_argument("--use-album-track-numbers", action="store_true", dest="use_album_track_numbers")
    parser.add_argument("--use-artist-subfolders",   action="store_true", dest="use_artist_subfolders")
    parser.add_argument("--use-album-subfolders",    action="store_true", dest="use_album_subfolders")
    parser.add_argument("--first-artist-only",       action="store_true", dest="first_artist_only")
    parser.add_argument(
        "--include-featuring",
        action  = "store_true",
        dest    = "include_featuring",
        default = False,
        help    = "Includi tracce dove l'artista appare come featuring su release di altri artisti.",
    )
    parser.add_argument("--qobuz-token", default=None, dest="qobuz_token", help="Token Qobuz")
    parser.add_argument("--loop", "-l", type=int, default=None, help="Ripeti ogni N minuti")
    parser.add_argument("--verbose", "-v", action="store_true")

    # ── Lyrics ──────────────────────────────────────────────────────────────
    lyrics_grp = parser.add_argument_group("Lyrics")
    lyrics_grp.add_argument(
        "--no-lyrics",
        action = "store_false",
        dest   = "embed_lyrics",
        help   = "Disabilita l'embedding dei testi (attivo di default)",
    )
    parser.set_defaults(embed_lyrics=True)

    lyrics_grp.add_argument(
        "--lyrics-providers",
        nargs   = "+",
        default = ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
        dest    = "lyrics_providers",
        choices = ["spotify", "apple", "musixmatch", "amazon", "lrclib"],
        help    = "Provider testi in ordine (default: spotify apple musixmatch lrclib amazon).",
    )
    lyrics_grp.add_argument(
        "--spotify-token",
        default = "",
        dest    = "spotify_token",
        metavar = "SP_DC",
        help    = "Cookie sp_dc Spotify",
    )

    # ── Metadata enrichment ─────────────────────────────────────────────────
    enrich_grp = parser.add_argument_group("Metadata Enrichment")
    enrich_grp.add_argument(
        "--no-enrich",
        action = "store_false",
        dest   = "enrich",
        help   = "Disabilita l'arricchimento metadati (attivo di default)",
    )
    parser.set_defaults(enrich=True)

    enrich_grp.add_argument(
        "--enrich-providers",
        nargs   = "+",
        default = ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
        dest    = "enrich_providers",
        choices = ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
        help    = "Provider metadata enrichment in ordine (default: deezer apple qobuz tidal soundcloud).",
    )

    return parser.parse_args()

def main() -> None:
    check_for_updates()

    if len(sys.argv) == 1:
        cfg = run_interactive()

        log_level = logging.WARNING
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        SpotiFLAC(
            url                      = cfg["url"],
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
            lyrics_spotify_token     = cfg.get("lyrics_spotify_token", ""),
            enrich_metadata          = cfg["enrich_metadata"],
            enrich_providers         = cfg["enrich_providers"],
            qobuz_token              = cfg.get("qobuz_token"),
            include_featuring        = cfg["include_featuring"],
        )

    else:
        args = parse_args()
        file_cfg = load_config()

        # FIX: le variabili merged vengono ora effettivamente usate nel call a SpotiFLAC().
        # Prima il codice calcolava quality/qobuz_token/spotify_token unificati
        # ma poi passava args.quality / args.qobuz_token / args.spotify_token (raw),
        # rendendo il merge da config.json completamente inutile.
        quality       = args.quality       or file_cfg.get("quality", "LOSSLESS")
        qobuz_token   = args.qobuz_token   or file_cfg.get("qobuz_token")
        spotify_token = args.spotify_token or file_cfg.get("spotify_token", "")

        log_level = logging.DEBUG if args.verbose else logging.WARNING
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        SpotiFLAC(
            url                      = args.url,
            output_dir               = args.output_dir,
            services                 = args.service,
            filename_format          = args.filename_format,
            use_track_numbers        = args.use_track_numbers,
            use_album_track_numbers  = args.use_album_track_numbers,
            use_artist_subfolders    = args.use_artist_subfolders,
            use_album_subfolders     = args.use_album_subfolders,
            loop                     = args.loop,
            quality                  = quality,         # FIX: era args.quality
            first_artist_only        = args.first_artist_only,
            log_level                = log_level,
            output_path              = args.output_path,
            embed_lyrics             = args.embed_lyrics,
            lyrics_providers         = args.lyrics_providers,
            lyrics_spotify_token     = spotify_token,   # FIX: era args.spotify_token
            enrich_metadata          = args.enrich,
            enrich_providers         = args.enrich_providers,
            qobuz_token              = qobuz_token,     # FIX: era args.qobuz_token
            include_featuring        = args.include_featuring,
        )

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
CLI entry point per SpotiFLAC — con supporto provider lyrics e metadata enrichment.
"""
import argparse
import logging

from SpotiFLAC import SpotiFLAC


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog            = "spotiflac",
        description     = "Download Spotify tracks in true FLAC via Tidal, Qobuz e altri.",
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )

    # Argomenti base
    parser.add_argument("url",        help="Spotify URL (track, album, o playlist)")
    parser.add_argument("output_dir", help="Directory di destinazione")

    parser.add_argument(
        "--service", "-s",
        choices = ["tidal", "qobuz", "deezer", "amazon", "youtube"],
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
        "--quality", "-q",
        default = "LOSSLESS",
        help    = "Quality: LOSSLESS or HI_RES (Tidal), 6/7/27 (Qobuz). Default: LOSSLESS",
    )
    parser.add_argument("--use-track-numbers",     action="store_true", dest="use_track_numbers")
    parser.add_argument("--use-artist-subfolders", action="store_true", dest="use_artist_subfolders")
    parser.add_argument("--use-album-subfolders",  action="store_true", dest="use_album_subfolders")
    parser.add_argument("--first-artist-only",     action="store_true", dest="first_artist_only")
    parser.add_argument(
        "--qobuz-token",
        default = None,
        dest    = "qobuz_token",
        metavar = "TOKEN",
        help    = "Token utente Qobuz (x-user-auth-token)",
    )
    parser.add_argument(
        "--loop", "-l",
        type    = int,
        default = None,
        metavar = "MINUTES",
        help    = "Ripeti ogni N minuti (default: singolo run)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    # ── Lyrics ──────────────────────────────────────────────────────────────
    lyrics_grp = parser.add_argument_group("Lyrics")
    lyrics_grp.add_argument(
        "--embed-lyrics",
        action = "store_true",
        dest   = "embed_lyrics",
        help   = "Embed testi nel file FLAC (tag LYRICS)",
    )
    lyrics_grp.add_argument(
        "--lyrics-providers",
        nargs   = "+",
        default = None,
        dest    = "lyrics_providers",
        choices = ["spotify", "musixmatch", "amazon", "lrclib"],
        metavar = "PROVIDER",
        help    = "Provider testi in ordine. Default: spotify musixmatch amazon lrclib",
    )
    lyrics_grp.add_argument(
        "--spotify-token",
        default = "",
        dest    = "spotify_token",
        metavar = "SP_DC",
        help    = "Cookie sp_dc Spotify (per testi Spotify Web)",
    )
    lyrics_grp.add_argument(
        "--musixmatch-token",
        default = "",
        dest    = "musixmatch_token",
        metavar = "TOKEN",
        help    = "Token desktop Musixmatch",
    )

    # ── Metadata enrichment ─────────────────────────────────────────────────
    enrich_grp = parser.add_argument_group("Metadata Enrichment")
    enrich_grp.add_argument(
        "--enrich",
        action = "store_true",
        dest   = "enrich",
        help   = "Arricchisci metadati da provider aggiuntivi (label, BPM, genere…)",
    )
    enrich_grp.add_argument(
        "--enrich-providers",
        nargs   = "+",
        default = None,
        dest    = "enrich_providers",
        choices = ["deezer", "apple", "qobuz", "tidal"],
        metavar = "PROVIDER",
        help    = "Provider metadata enrichment in ordine. Default: deezer apple qobuz tidal",
    )

    return parser.parse_args()


def main() -> None:
    args      = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.WARNING

    SpotiFLAC(
        url                      = args.url,
        output_dir               = args.output_dir,
        services                 = args.service,
        filename_format          = args.filename_format,
        use_track_numbers        = args.use_track_numbers,
        use_artist_subfolders    = args.use_artist_subfolders,
        use_album_subfolders     = args.use_album_subfolders,
        loop                     = args.loop,
        quality                  = args.quality,
        first_artist_only        = args.first_artist_only,
        log_level                = log_level,
        # Lyrics
        embed_lyrics             = args.embed_lyrics,
        lyrics_providers         = args.lyrics_providers,
        lyrics_spotify_token     = args.spotify_token,
        lyrics_musixmatch_token  = args.musixmatch_token,
        # Enrichment
        enrich_metadata          = args.enrich,
        enrich_providers         = args.enrich_providers,
        qobuz_token              = args.qobuz_token,
    )


if __name__ == "__main__":
    main()
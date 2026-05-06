#!/usr/bin/env python3
"""
CLI entry point per SpotiFLAC — con supporto provider lyrics e metadata enrichment ATTIVI di default.
"""
import argparse
import logging
import sys

from SpotiFLAC.check_update import check_for_updates
from SpotiFLAC import SpotiFLAC
from SpotiFLAC.interactive import run_interactive


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
        choices = ["tidal", "qobuz", "amazon", "spoti", "soundcloud"],
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
        help    = "Quality: LOSSLESS or HI_RES. Default: LOSSLESS",
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
        default = ["spotify", "musixmatch", "lrclib", "apple"],
        dest    = "lyrics_providers",
        choices = ["spotify", "apple", "musixmatch", "amazon", "lrclib"],
        help    = "Provider testi in ordine (default: spotify musixmatch lrclib apple).",
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
        default = ["deezer", "apple", "qobuz", "tidal"],
        dest    = "enrich_providers",
        choices = ["deezer", "apple", "qobuz", "tidal"],
        help    = "Provider metadata enrichment in ordine (default: deezer apple qobuz tidal).",
    )

    return parser.parse_args()

def main() -> None:
    # Esegui il controllo degli aggiornamenti a prescindere dalla modalità
    check_for_updates()

    # ── RILEVAMENTO AUTOMATICO ──────────────────────────────────────────────
    # Se la lista degli argomenti contiene solo il nome dello script (len == 1)
    # l'utente non ha passato flag, quindi invochiamo la modalità interattiva.
    if len(sys.argv) == 1:
        # Avvia il wizard interattivo
        cfg = run_interactive()

        # Imposta un livello di log di default per la modalità interattiva
        log_level = logging.WARNING
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Lancia l'istanza passando le chiavi del dizionario 'cfg' raccolte dal wizard
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
            # Lyrics
            embed_lyrics             = cfg["embed_lyrics"],
            lyrics_providers         = cfg["lyrics_providers"],
            lyrics_spotify_token     = cfg.get("lyrics_spotify_token", ""),
            # Enrichment
            enrich_metadata          = cfg["enrich_metadata"],
            enrich_providers         = cfg["enrich_providers"],
            qobuz_token              = cfg.get("qobuz_token"),
            include_featuring        = cfg["include_featuring"],
        )

    # ── MODALITÀ CLI CLASSICA ───────────────────────────────────────────────
    else:
        # Passa ad argparse il parsing standard
        args = parse_args()

        log_level = logging.DEBUG if args.verbose else logging.WARNING
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Lancia l'istanza usando il Namespace 'args'
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
            quality                  = args.quality,
            first_artist_only        = args.first_artist_only,
            log_level                = log_level,
            output_path              = args.output_path,
            # Lyrics
            embed_lyrics             = args.embed_lyrics,
            lyrics_providers         = args.lyrics_providers,
            lyrics_spotify_token     = args.spotify_token,
            # Enrichment
            enrich_metadata          = args.enrich,
            enrich_providers         = args.enrich_providers,
            qobuz_token              = args.qobuz_token,
            include_featuring        = args.include_featuring,
        )

if __name__ == "__main__":
    main()
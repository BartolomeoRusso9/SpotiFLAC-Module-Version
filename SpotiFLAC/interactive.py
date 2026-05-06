"""
SpotiFLAC — Modalità interattiva.
Guida l'utente nella configurazione step-by-step senza dover ricordare i flag CLI.
"""
from __future__ import annotations
import os
import sys

# ---------------------------------------------------------------------------
# ANSI colors (funzionano su macOS, Linux, Windows 10+)
# ---------------------------------------------------------------------------
_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

BOLD   = lambda t: _c("1", t)
DIM    = lambda t: _c("2", t)
CYAN   = lambda t: _c("96", t)
GREEN  = lambda t: _c("92", t)
YELLOW = lambda t: _c("93", t)
RED    = lambda t: _c("91", t)
BLUE   = lambda t: _c("94", t)
MAGENTA= lambda t: _c("95", t)


# ---------------------------------------------------------------------------
# Primitive input helpers
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    """Chiede una stringa all'utente."""
    default_hint = f" {DIM('[' + default + ']')}" if default else ""
    try:
        val = input(f"  {prompt}{default_hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def _ask_bool(prompt: str, default: bool = False) -> bool:
    """Chiede sì/no."""
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
    """Chiede di scegliere una opzione dalla lista."""
    print(f"\n  {BOLD(prompt)}")
    for i, opt in enumerate(options, 1):
        marker = GREEN("▶") if opt == default else " "
        print(f"    {marker} {DIM(f'[{i}]')} {opt}")
    print(f"    {DIM('Invio = default')}")
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
    """
    Chiede di selezionare più opzioni.
    Se ordered=True, l'ordine di inserimento viene mantenuto.
    """
    print(f"\n  {BOLD(prompt)}")
    for i, opt in enumerate(options, 1):
        marker = GREEN("●") if opt in defaults else DIM("○")
        default_label = DIM(" (default)") if opt in defaults else ""
        print(f"    {DIM(f'[{i}]')} {marker} {opt}{default_label}")
    print(f"    {DIM('Inserisci i numeri separati da spazio (es: 1 3 2) — Invio = default')}")
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


# ---------------------------------------------------------------------------
# Sezioni del wizard
# ---------------------------------------------------------------------------

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
    print(f"  {DIM('Premi Ctrl+C in qualsiasi momento per uscire')}")


def _summary(cfg: dict) -> None:
    _section("Riepilogo configurazione")

    def row(label: str, value: str) -> None:
        print(f"  {BOLD(label + ':'): <30} {GREEN(value)}")

    row("URL", cfg["url"])
    row("Output", cfg["output_dir"])
    row("Servizi", " → ".join(cfg["services"]))
    row("Qualità", cfg["quality"])
    row("Formato filename", cfg["filename_format"])

    flags = []
    if cfg["use_track_numbers"]:     flags.append("track-numbers")
    if cfg["use_album_track_numbers"]: flags.append("album-track-numbers")
    if cfg["use_artist_subfolders"]: flags.append("artist-subfolders")
    if cfg["use_album_subfolders"]:  flags.append("album-subfolders")
    if cfg["first_artist_only"]:     flags.append("first-artist-only")
    if cfg["include_featuring"]:     flags.append("include-featuring")
    row("Opzioni", ", ".join(flags) if flags else "nessuna")

    row("Testi", "abilitati (" + ", ".join(cfg["lyrics_providers"]) + ")" if cfg["embed_lyrics"] else "disabilitati")
    row("Enrichment", "abilitato (" + ", ".join(cfg["enrich_providers"]) + ")" if cfg["enrich_metadata"] else "disabilitato")

    if cfg.get("qobuz_token"):
        row("Qobuz token", "✓ impostato")
    if cfg.get("lyrics_spotify_token"):
        row("Spotify token", "✓ impostato")
    if cfg.get("loop"):
        row("Loop", f"ogni {cfg['loop']} minuti")


# ---------------------------------------------------------------------------
# Wizard principale
# ---------------------------------------------------------------------------

def run_interactive() -> dict:
    """
    Esegue il wizard interattivo e restituisce un dict di configurazione
    compatibile con i parametri di SpotiFLAC().
    """
    _header()

    cfg: dict = {}

    # ── 1. URL ──────────────────────────────────────────────────────────────
    _section("1 · URL")
    print(f"  {DIM('Supportati: Spotify e Tidal (track, album, playlist, artista)')}")
    url = ""
    while not url:
        url = _ask("URL")
        if not url:
            print(f"  {RED('⚠  URL obbligatorio.')}")
    cfg["url"] = url

    # ── 2. Output directory ─────────────────────────────────────────────────
    _section("2 · Directory di output")
    cfg["output_dir"] = _ask("Cartella di destinazione", "./Downloads")

    # ── 3. Servizi ──────────────────────────────────────────────────────────
    _section("3 · Servizi audio")
    print(f"  {DIM('Scegli i servizi e il loro ordine di priorità (il primo ha la precedenza)')}")
    services = _ask_multi(
        "Servizi (ordine = priorità):",
        options  = ["tidal", "qobuz", "amazon", "spoti"],
        defaults = ["tidal"],
        ordered  = True,
    )
    cfg["services"] = services

    # ── 4. Qualità ──────────────────────────────────────────────────────────
    _section("4 · Qualità audio")
    print(f"  {DIM('Nota: Se la qualità richiesta non viene trovata, verrà eseguito un fallback automatico.')}")

    has_qobuz = "qobuz" in cfg["services"]
    has_tidal = "tidal" in cfg["services"]

    if has_qobuz and not has_tidal:
        # L'utente ha scelto Qobuz ma non Tidal
        q_choice = _ask_choice(
            "Qualità Qobuz:",
            options = ["6 (CD Lossless)", "7 (Hi-Res)", "27 (Hi-Res Max)"],
            default = "6 (CD Lossless)",
        )
        # Estraiamo solo il numero (6, 7 o 27) dalla stringa scelta
        cfg["quality"] = q_choice.split(" ")[0]

    elif has_tidal and not has_qobuz:
        # L'utente ha scelto Tidal ma non Qobuz
        cfg["quality"] = _ask_choice(
            "Qualità Tidal:",
            options = ["LOSSLESS", "HI_RES"],
            default = "LOSSLESS",
        )

    elif has_qobuz and has_tidal:
        # L'utente ha inserito entrambi i provider nella lista
        print(f"  {DIM('Hai selezionato sia Qobuz che Tidal. Scegli un profilo unificato:')}")
        q_choice = _ask_choice(
            "Qualità combinata:",
            options = [
                "LOSSLESS (Applica '6' su Qobuz e 'LOSSLESS' su Tidal)",
                "HI_RES (Applica '27' su Qobuz e 'HI_RES' su Tidal)",
                "7 (Applica qualità Hi-Res intermedia solo per Qobuz)"
            ],
            default = "LOSSLESS (Applica '6' su Qobuz e 'LOSSLESS' su Tidal)",
        )
        # Riduciamo la stringa lunga al valore chiave richiesto dalla CLI
        if q_choice.startswith("LOSSLESS"):
            cfg["quality"] = "LOSSLESS"
        elif q_choice.startswith("HI_RES"):
            cfg["quality"] = "HI_RES"
        else:
            cfg["quality"] = "7"

    else:
        # Fallback nel caso usi solo Amazon o Spotify
        cfg["quality"] = _ask_choice(
            "Qualità:",
            options = ["LOSSLESS", "HI_RES"],
            default = "LOSSLESS",
        )

    # ── 5. Formato filename ─────────────────────────────────────────────────
    _section("5 · Formato nome file")
    print(f"  {DIM('Placeholder: {title} {artist} {album} {album_artist} {year} {date} {track} {disc} {isrc} {position}')}")
    cfg["filename_format"] = _ask("Formato", "{title} - {artist}")

    # ── 6. Opzioni organizzazione ───────────────────────────────────────────
    _section("6 · Opzioni organizzazione")
    cfg["use_track_numbers"]       = _ask_bool("Aggiungi numero traccia al nome file?", False)
    cfg["use_album_track_numbers"] = _ask_bool("Usa numero traccia originale dell'album?", False)
    cfg["use_artist_subfolders"]   = _ask_bool("Crea sottocartelle per artista?", False)
    cfg["use_album_subfolders"]    = _ask_bool("Crea sottocartelle per album?", False)
    cfg["first_artist_only"]       = _ask_bool("Usa solo il primo artista nei tag e nel nome?", False)

    # ── 7. Featuring ────────────────────────────────────────────────────────
    _section("7 · Featuring")
    print(f"  {DIM('Se attivato, scarica anche le singole tracce dove artista appare come featured')}")
    print(f"  {DIM('su release di altri artisti (applies_on su Spotify, compilazioni su Tidal)')}")
    cfg["include_featuring"] = _ask_bool("Includi tracce featuring?", False)

    # ── 8. Testi ────────────────────────────────────────────────────────────
    _section("8 · Testi (Lyrics)")
    cfg["embed_lyrics"] = _ask_bool("Incorpora i testi sincronizzati?", True)

    if cfg["embed_lyrics"]:
        cfg["lyrics_providers"] = _ask_multi(
            "Provider testi (ordine = priorità):",
            options  = ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
            defaults = ["spotify", "musixmatch", "lrclib", "apple"],
            ordered  = True,
        )
        has_spotify = "spotify" in cfg["lyrics_providers"]
        if has_spotify:
            print(f"  {DIM('Il provider Spotify richiede il cookie sp_dc per i testi sincronizzati')}")
            token = _ask("Cookie sp_dc Spotify (lascia vuoto per saltare)", "")
            cfg["lyrics_spotify_token"] = token
        else:
            cfg["lyrics_spotify_token"] = ""
    else:
        cfg["lyrics_providers"]      = ["spotify", "musixmatch", "lrclib", "apple"]
        cfg["lyrics_spotify_token"]  = ""

    # ── 9. Metadata enrichment ──────────────────────────────────────────────
    _section("9 · Arricchimento metadati")
    print(f"  {DIM('Aggiunge genere, BPM, etichetta, cover HD, MusicBrainz IDs e altro')}")
    cfg["enrich_metadata"] = _ask_bool("Abilita arricchimento metadati?", True)

    if cfg["enrich_metadata"]:
        cfg["enrich_providers"] = _ask_multi(
            "Provider enrichment (ordine = priorità):",
            options  = ["deezer", "apple", "qobuz", "tidal"],
            defaults = ["deezer", "apple", "qobuz", "tidal"],
            ordered  = True,
        )
    else:
        cfg["enrich_providers"] = ["deezer", "apple", "qobuz", "tidal"]

    # ── 10. Token Qobuz ─────────────────────────────────────────────────────
    _section("10 · Token opzionali")
    cfg["qobuz_token"] = _ask("Qobuz auth token (lascia vuoto per saltare)", "") or None

    # ── 11. Loop ────────────────────────────────────────────────────────────
    loop_str = _ask("Ripeti ogni N minuti (lascia vuoto per disabilitare)", "")
    cfg["loop"] = int(loop_str) if loop_str.isdigit() else None

    # ── Riepilogo + conferma ─────────────────────────────────────────────────
    _summary(cfg)
    print()
    if not _ask_bool(BOLD("Avviare il download con questa configurazione?"), True):
        print(f"\n  {YELLOW('Operazione annullata.')}\n")
        sys.exit(0)

    # ── Comando CLI equivalente ──────────────────────────────────────────────
    _section("Comando CLI equivalente")
    _print_cli_command(cfg)

    return cfg


def _print_cli_command(cfg: dict) -> None:
    """Stampa il comando CLI equivalente alla configurazione scelta."""
    parts = [f'spotiflac "{cfg["url"]}" "{cfg["output_dir"]}"']
    parts.append(f'-s {" ".join(cfg["services"])}')
    if cfg["quality"] != "LOSSLESS":
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
        if cfg.get("lyrics_spotify_token"):
            parts.append(f'--spotify-token "{cfg["lyrics_spotify_token"]}"')
    if not cfg["enrich_metadata"]:
        parts.append("--no-enrich")
    else:
        parts.append(f'--enrich-providers {" ".join(cfg["enrich_providers"])}')
    if cfg.get("qobuz_token"):
        parts.append(f'--qobuz-token "{cfg["qobuz_token"]}"')
    if cfg.get("loop"):
        parts.append(f'--loop {cfg["loop"]}')

    cmd = " \\\n    ".join(parts)
    print(f"\n  {DIM(cmd)}\n")
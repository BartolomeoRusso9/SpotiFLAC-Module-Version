"""
Output centralizzato per messaggi utente (non logging).
Separa i messaggi user-facing dai logger di debug.
"""
from __future__ import annotations
import sys
import time

# Larghezza fissa per il banner sorgente
_BANNER_WIDTH = 60


def print_track_header(position: int, total: int, title: str, artists: str, album: str) -> None:
    bar   = "─" * _BANNER_WIDTH
    pos   = f"[{position}/{total}]"
    print(f"\n┌{bar}┐")
    print(f"│ {pos} {artists[:40]!s:<40} │")
    print(f"│   ↳ {title[:50]!s:<50} │")
    print(f"│   ↳ {album[:50]!s:<50} │")
    print(f"└{bar}┘")


def print_source_banner(provider: str, api: str, quality: str) -> None:
    label = _shorten_api(api)
    line  = f"  📡  {provider.upper()}  ·  {label}  ·  {quality}"
    print(f"{'─'*_BANNER_WIDTH}")
    print(f"{line}")
    print(f"{'─'*_BANNER_WIDTH}")


def print_official_source(provider: str, quality: str) -> None:
    line = f"  💎  {provider.upper()}  ·  API Ufficiale  ·  {quality}"
    print(f"{'─'*_BANNER_WIDTH}")
    print(f"{line}")
    print(f"{'─'*_BANNER_WIDTH}")


def print_summary(
        total:     int,
        succeeded: int,
        failed:    list[tuple[str, str, str]],
        elapsed_s: float,
) -> None:
    bar = "═" * _BANNER_WIDTH
    print(f"\n╔{bar}╗")
    print(f"║  📊 RIEPILOGO SESSIONE{'':<38}║")
    print(f"╠{bar}╣")
    print(f"║  Tracce totali : {total:<42}║")
    print(f"║  Completate    : {succeeded:<42}║")
    print(f"║  Fallite       : {len(failed):<42}║")
    print(f"║  Tempo impiegato: {_fmt_seconds(elapsed_s):<41}║")
    if failed:
        print(f"╠{bar}╣")
        print(f"║  ✗ FALLIMENTI{'':<47}║")
        for title, artists, err in failed:
            short = f"{title[:22]} — {artists[:16]}: {err[:14]}"
            print(f"║    {short:<56}║")
    print(f"╚{bar}╝")


def print_skip(filepath: str, size_mb: float) -> None:
    print(f"  ⏭  già presente  ·  {filepath[-45:]!s}  ({size_mb:.1f} MB)")


def print_api_failure(provider: str, api: str, reason: str) -> None:
    label = _shorten_api(api)
    print(f"  ✗  {provider}  ·  {label}  ·  {reason}", file=sys.stderr)


def print_quality_fallback(provider: str, from_q: str, to_q: str) -> None:
    print(f"  ⬇  {provider}: qualità {from_q} non disponibile — fallback → {to_q}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _shorten_api(url: str) -> str:
    """Ritorna solo il dominio, senza schema e path."""
    return url.removeprefix("https://").removeprefix("http://").split("/")[0]


def _fmt_seconds(s: float) -> str:
    s = int(round(s))
    parts = []
    for unit, div in [("h", 3600), ("m", 60), ("s", 1)]:
        val, s = divmod(s, div)
        if val:
            parts.append(f"{val}{unit}")
    return " ".join(parts) or "0s"
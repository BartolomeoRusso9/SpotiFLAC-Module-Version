"""
Centralized output for user-facing messages (not debug logging).
Separates user-facing console UI from debug loggers.
"""
from __future__ import annotations
import sys

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
    line = f"  💎  {provider.upper()}  ·  Official API  ·  {quality}"
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
    print(f"║  SESSION SUMMARY{'':<43}║")
    print(f"╠{bar}╣")
    print(f"║  Total Tracks  : {total:<42}║")
    print(f"║  Successful    : {succeeded:<42}║")
    print(f"║  Failed        : {len(failed):<42}║")
    print(f"║  Time Elapsed  : {_fmt_seconds(elapsed_s):<42}║")
    if failed:
        print(f"╠{bar}╣")
        print(f"║  ✗ FAILURES{'':<47}║")
        for title, artists, err in failed:
            short_err = _clean_error(err)[:18]
            short = f"{title[:20]} — {artists[:14]}: {short_err}"
            print(f"║    {short:<56}║")
    print(f"╚{bar}╝")

def print_skip(filepath: str, size_mb: float) -> None:
    print(f"  ⏭  already exists  ·  {filepath[-40:]!s}  ({size_mb:.1f} MB)")

def print_api_failure(provider: str, api: str, reason: str) -> None:
    label = _shorten_api(api)
    clean_reason = _clean_error(reason)
    print(f"  ✗  {provider}  ·  {label}  ·  {clean_reason}", file=sys.stderr)

def print_quality_fallback(provider: str, from_q: str, to_q: str) -> None:
    print(f"  ⬇  {provider}: quality {from_q} unavailable — falling back to {to_q}")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _shorten_api(url: str) -> str:
    return url.removeprefix("https://").removeprefix("http://").split("/")[0]

def _fmt_seconds(s: float) -> str:
    s = int(round(s))
    parts = []
    for unit, div in [("h", 3600), ("m", 60), ("s", 1)]:
        val, s = divmod(s, div)
        if val:
            parts.append(f"{val}{unit}")
    return " ".join(parts) or "0s"

def _clean_error(err: str) -> str:
    """Rimuove la spazzatura dalle eccezioni Python per l'output in console."""
    err_str = str(err)
    if "Max retries exceeded" in err_str or "NameResolutionError" in err_str:
        return "Connection timeout / Unreachable"
    if "Read timed out" in err_str:
        return "Read timed out"
    if "403 Client Error: Forbidden" in err_str:
        return "HTTP 403 Forbidden (Cloudflare/WAF blocked)"
    if "Expecting value: line 1" in err_str or "invalid JSON" in err_str.lower():
        return "Invalid JSON response"
    return err_str.split('\n')[0][:60]
from __future__ import annotations

import sys

from tqdm import tqdm

_BANNER_WIDTH = 60
_MAX_API_FAILURES_PER_PROVIDER = 20
_api_failure_state: dict[str, dict[str, object]] = {}


def _write(line: str) -> None:
    """Writes one console line without tearing an active progress bar."""
    with tqdm.get_lock():
        tqdm.write(line, file=sys.stderr)


def _reset_api_failure_state() -> None:
    global _api_failure_state
    _api_failure_state = {}


def _should_print_api_failure(provider: str, api: str, reason: str) -> bool:
    normalized_reason = _clean_error(reason)
    provider_state = _api_failure_state.setdefault(
        provider,
        {
            "seen": set(),
            "printed": 0,
            "suppressed": 0,
            "summary_shown": False,
        },
    )
    key = (api, normalized_reason)
    if key in provider_state["seen"]:
        return False
    provider_state["seen"].add(key)
    if provider_state["printed"] < _MAX_API_FAILURES_PER_PROVIDER:
        provider_state["printed"] += 1
        return True
    provider_state["suppressed"] += 1
    return False


def _maybe_print_api_failure_summary(provider: str) -> None:
    provider_state = _api_failure_state.get(provider)
    if provider_state is None or provider_state["suppressed"] == 0:
        return
    if provider_state["summary_shown"]:
        return
    provider_state["summary_shown"] = True
    suppressed = provider_state["suppressed"]
    _write(f"  ... {suppressed} more {provider} API failures suppressed")


def print_run_header(
    total: int,
    services: list[str],
    quality: str,
    output_dir: str,
    concurrency: int,
) -> None:
    """Announces what the run is about to do, before the first request.

    Worth a line of its own: when a download later goes wrong, the settings
    it ran with are the first thing needed to make sense of it, and in a log
    file they are otherwise nowhere to be found.
    """
    _write(
        f"[RUN] {total} track(s) · {', '.join(services) or 'no provider'} · "
        f"{quality} · {concurrency} in parallel → {output_dir}",
    )


def print_track_header(
    position: int,
    total: int,
    title: str,
    artists: str,
    album: str,
) -> None:
    _reset_api_failure_state()
    pos = f"[{position}/{total}]"
    summary = f"Track {pos} {title[:40]!s} — {artists[:40]!s} ({album[:32]!s})"
    _write(summary)


def print_track_progress(
    track_name: str,
    percent: int,
    current_bytes: int,
    total_bytes: int,
) -> None:
    """Progress of a single track, as text rather than as a moving bar."""
    _write(
        f"  ⬇  {track_name[:40]}  ·  {percent}%  ·  "
        f"{format_bytes(current_bytes)} / {format_bytes(total_bytes)}",
    )


def print_track_done(
    provider: str,
    title: str,
    fmt: str,
    size_bytes: float,
    elapsed_s: float,
) -> None:
    """Outcome of a finished track: where it came from and what landed."""
    details = [provider.upper(), (fmt or "flac").upper()]
    if size_bytes > 0:
        details.append(format_bytes(size_bytes))
    details.append(_fmt_seconds(elapsed_s))
    _write(f"  ✓  {title[:40]}  ·  {'  ·  '.join(details)}")


def print_track_skipped(title: str, reason: str) -> None:
    _write(f"  ⏭  {title[:40]}  ·  {reason}")


def print_source_banner(provider: str, api: str, quality: str) -> None:
    provider_label = provider.upper()

    if api:
        line = f"[SOURCE] {provider_label} · {_shorten_api(provider, api)} · {quality}"
    else:
        line = f"[SOURCE] {provider_label} · {quality}"

    _write(line)


def print_official_source(provider: str, quality: str) -> None:
    _write(f"[SOURCE] {provider.upper()} · Official API · {quality}")


def print_summary(
    total: int,
    succeeded: int,
    skipped: int,
    failed: list[tuple[str, str, str]],
    elapsed_s: float,
) -> None:
    bar = "═" * _BANNER_WIDTH
    summary = f"\n╔{bar}╗\n"
    summary += f"║  SESSION SUMMARY{'':<43}║\n"
    summary += f"╠{bar}╣\n"
    summary += f"║  Total Tracks  : {total:<42}║\n"
    summary += f"║  Successful    : {succeeded:<42}║\n"
    summary += f"║  Skipped       : {skipped:<42}║\n"
    summary += f"║  Failed        : {len(failed):<42}║\n"
    summary += f"║  Time Elapsed  : {_fmt_seconds(elapsed_s):<42}║"

    if failed:
        summary += f"\n╠{bar}╣\n"
        summary += f"║  ✗ FAILURES{'':<47}║\n"
        for title, artists, err in failed:
            short_err = _clean_error(err)[:18]
            short = f"{title[:20]} — {artists[:14]}: {short_err}"
            summary += f"\n║    {short:<56}║"
    summary += f"\n╚{bar}╝"
    _write(summary)


def print_playlist_resolved(name: str, track_count: int, url: str) -> None:
    """One line per playlist as it is fetched.

    A sync that resolves four playlists and then goes quiet for ten minutes
    is indistinguishable from one that hung on the first; this says which
    ones were read, and how big each turned out to be.
    """
    _write(f"[PLAYLIST] {name[:48]} · {track_count} track(s) · {url}")


def print_sync_plan(unique: int, already_present: int, pending: int) -> None:
    """What the merge decided, before a single byte is downloaded."""
    _write(
        f"[SYNC] {unique} unique track(s) · {already_present} already on disk · "
        f"{pending} to download",
    )


def print_playlist_summary(
    rows: list[tuple[str, str, int, int]],
    unique_tracks: int,
    already_present: int,
) -> None:
    """Prints the per-playlist outcome of a multi-playlist sync.

    Each row is (playlist name, M3U status, tracks listed, tracks missing).
    """
    bar = "═" * _BANNER_WIDTH
    summary = f"\n╔{bar}╗\n"
    summary += f"║  PLAYLIST SYNC{'':<45}║\n"
    summary += f"╠{bar}╣\n"
    summary += f"║  Unique Tracks : {unique_tracks:<42}║\n"
    summary += f"║  Already There : {already_present:<42}║"

    for name, status, listed, missing in rows:
        summary += f"\n╠{bar}╣\n"
        summary += f"║  {name[:56]:<58}║\n"
        detail = f"{status} · {listed} track(s)"
        if missing:
            detail += f" · {missing} missing"
        summary += f"║    {detail[:56]:<56}║"
    summary += f"\n╚{bar}╝"
    _write(summary)


def print_api_failure(provider: str, api: str, reason: str) -> None:
    _write(
        f"  ✗  {provider}  ·  {_shorten_api(provider, api)}  ·  {_clean_error(reason)}",
    )


def print_quality_fallback(provider: str, from_q: str, to_q: str) -> None:
    _write(f"  ⬇  {provider}: quality {from_q} unavailable — falling back to {to_q}")


def _shorten_api(provider: str, url: str) -> str:
    return (
        url.removeprefix("https://").removeprefix("http://").split("/")[0].split(".")[0]
    )


def format_bytes(num: float) -> str:
    """Byte count as humans read it, e.g. '28.4 MB'."""
    for unit in ("B", "KB", "MB"):
        if abs(num) < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _fmt_seconds(s: float) -> str:
    s = round(s)
    parts = []
    for unit, div in [("h", 3600), ("m", 60), ("s", 1)]:
        val, s = divmod(s, div)
        if val:
            parts.append(f"{val}{unit}")
    return " ".join(parts) or "0s"


def _clean_error(err: str) -> str:
    err_str = str(err)
    if "Max retries exceeded" in err_str or "NameResolutionError" in err_str:
        return "Connection timeout / Unreachable"
    if (
        "nodename nor servname provided" in err_str
        or "Name or service not known" in err_str
    ):
        return "DNS resolution failed"
    if "Read timed out" in err_str or "Timeout" in err_str:
        return "Read timed out"
    if "HTTP 503" in err_str:
        return "HTTP 503 Service Unavailable"
    if "HTTP 502" in err_str:
        return "HTTP 502 Bad Gateway"
    if "HTTP 404" in err_str:
        return "HTTP 404 Not Found"
    if "HTTP 400" in err_str:
        return "HTTP 400 Bad Request"
    if "403 Client Error: Forbidden" in err_str:
        return "HTTP 403 Forbidden (Cloudflare/WAF blocked)"
    if "Expecting value: line 1" in err_str or "invalid JSON" in err_str.lower():
        return "Invalid JSON response"
    return err_str.split("\n", maxsplit=1)[0][:60]

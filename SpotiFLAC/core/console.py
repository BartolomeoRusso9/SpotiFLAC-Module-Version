from __future__ import annotations
import sys
from tqdm import tqdm 

_BANNER_WIDTH = 60
_MAX_API_FAILURES_PER_PROVIDER = 20
_api_failure_state: dict[str, dict[str, object]] = {}


def _reset_api_failure_state() -> None:
    global _api_failure_state
    _api_failure_state = {}


def _should_print_api_failure(provider: str, api: str, reason: str) -> bool:
    normalized_reason = _clean_error(reason)
    provider_state = _api_failure_state.setdefault(provider, {
        "seen": set(),
        "printed": 0,
        "suppressed": 0,
        "summary_shown": False,
    })
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
    with tqdm.get_lock():
        tqdm.write(f"  ... {suppressed} more {provider} API failures suppressed", file=sys.stderr)


def print_track_header(position: int, total: int, title: str, artists: str, album: str) -> None:
    _reset_api_failure_state()
    pos = f"[{position}/{total}]"
    summary = f"Track {pos} {title[:40]!s} — {artists[:40]!s} ({album[:32]!s})"
    with tqdm.get_lock():
        tqdm.write(summary, file=sys.stderr)

def print_source_banner(provider: str, api: str, quality: str) -> None:
    provider_label = provider.upper()

    if api:
        line = f"[SOURCE] {provider_label} · {_shorten_api(provider, api)} · {quality}"
    else:
        line = f"[SOURCE] {provider_label} · {quality}"

    with tqdm.get_lock():
        tqdm.write(line, file=sys.stderr)

def print_official_source(provider: str, quality: str) -> None:
    line = f"[SOURCE] {provider.upper()} · Official API · {quality}"
    with tqdm.get_lock():
        tqdm.write(line, file=sys.stderr)

def print_summary(total: int, succeeded: int, failed: list[tuple[str, str, str]], elapsed_s: float) -> None:
    bar = "═" * _BANNER_WIDTH
    summary = f"\n╔{bar}╗\n"
    summary += f"║  SESSION SUMMARY{'':<43}║\n"
    summary += f"╠{bar}╣\n"
    summary += f"║  Total Tracks  : {total:<42}║\n"
    summary += f"║  Successful    : {succeeded:<42}║\n"
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
    with tqdm.get_lock():
        tqdm.write(summary, file=sys.stderr)

def print_api_failure(provider: str, api: str, reason: str) -> None:
    with tqdm.get_lock():
        tqdm.write(
            f"  ✗  {provider}  ·  {_shorten_api(provider, api)}  ·  {_clean_error(reason)}",
            file=sys.stderr,
        )

def print_quality_fallback(provider: str, from_q: str, to_q: str) -> None:
    with tqdm.get_lock():
        tqdm.write(f"  ⬇  {provider}: quality {from_q} unavailable — falling back to {to_q}", file=sys.stderr)

def _shorten_api(provider: str, url: str) -> str:
    return url.removeprefix("https://") \
              .removeprefix("http://") \
              .split("/")[0] \
              .split(".")[0]

def _fmt_seconds(s: float) -> str:
    s = int(round(s))
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
    if "nodename nor servname provided" in err_str or "Name or service not known" in err_str:
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
    return err_str.split('\n')[0][:60]
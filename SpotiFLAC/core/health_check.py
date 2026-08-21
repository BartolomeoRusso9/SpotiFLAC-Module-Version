"""Health checks for direct lyrics provider servers."""

from __future__ import annotations

import asyncio
import time
from typing import NamedTuple

import httpx

from . import get_amazon_endpoint

_UA = "SpotiFLAC-HealthCheck/5.0"
_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
_AMAZON_LYRICS_URL = get_amazon_endpoint("spotbye1")
_SERVERS = {
    "apple": "https://lyrics.paxsenix.org/apple-music/lyrics?id=1440650711",
    "lrclib": "https://lrclib.net/api/get?artist_name=Queen&track_name=Bohemian%20Rhapsody",
    "musixmatch": "https://lyrics.paxsenix.org/musixmatch/lyrics?type=word&t=Bohemian%20Rhapsody&a=Queen&d=355&format=lrc",
    "spotify": "https://spclient.wg.spotify.com/color-lyrics/v2/track/0UHB9METy4VCXNgkcGqHqS?format=json&market=from_token",
    "amazon": (
        f"{_AMAZON_LYRICS_URL.rstrip('/')}/lyrics/QM5FT1600115"
        if _AMAZON_LYRICS_URL
        else "https://invalid.local/amazon-lyrics"
    ),
    "deezer": "https://lyrics.paxsenix.org/deezer/lyrics?id=3135556",
    "genius": "https://lyrics.paxsenix.org/genius/lyrics?url=https%3A%2F%2Fgenius.com%2FQueen-bohemian-rhapsody-lyrics",
    "netease": "https://lyrics.paxsenix.org/netease/search?q=Bohemian%20Rhapsody%20Queen",
    "qq": "https://lyrics.paxsenix.org/qq/search?q=Bohemian%20Rhapsody%20Queen",
    "youtube": "https://lyrics.paxsenix.org/youtube/search?q=Bohemian%20Rhapsody%20Queen",
    "kugou": "https://lyrics.paxsenix.org/kugou/search?q=Bohemian%20Rhapsody%20Queen",
}


class HealthResult(NamedTuple):
    provider: str
    url: str
    method: str
    ok: bool
    latency: float
    detail: str


async def _probe(client: httpx.AsyncClient, name: str, url: str) -> HealthResult:
    started = time.perf_counter()
    try:
        response = await client.get(
            url,
            headers={"User-Agent": _UA, "Accept": "application/json,text/plain"},
            follow_redirects=True,
        )
        latency = (time.perf_counter() - started) * 1000
        if 200 <= response.status_code < 300:
            return HealthResult(
                name, url, "GET", True, latency, f"HTTP {response.status_code}"
            )
        return HealthResult(
            name, url, "GET", False, latency, f"HTTP {response.status_code}"
        )
    except httpx.TimeoutException:
        return HealthResult(name, url, "GET", False, -1, "timeout")
    except httpx.RequestError as exc:
        return HealthResult(name, url, "GET", False, -1, str(exc)[:40])
    except Exception as exc:
        return HealthResult(name, url, "GET", False, -1, str(exc)[:40])


async def run_health_check(
    services: list[str] | None = None,
    *,
    include_all_endpoints: bool = True,
) -> list[HealthResult]:
    """Check direct lyrics, Spotify and MusicBrainz server reachability."""
    del services, include_all_endpoints
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            *(_probe(client, name, url) for name, url in _SERVERS.items())
        )
    return list(results)


def print_health_report(
    results: list[HealthResult],
    *,
    show_urls: bool = True,
) -> None:
    """Print a compact health report for direct servers."""
    for result in results:
        suffix = f" {result.url}" if show_urls else ""
        state = "OK" if result.ok else "FAIL"
        print(f"{result.provider}: {state} ({result.detail}){suffix}")

"""Health checks for direct lyrics provider servers."""

from __future__ import annotations

import asyncio
import time
from typing import NamedTuple

import httpx

_UA = "Mornye-HealthCheck/5.0"
_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
_SERVERS = {
    "apple": "https://api.zarz.moe/apple-music/lyrics?id=6781024437&format=elrc",
    "lyricsplus": "https://lyricsplus.prjktla.my.id/v1/lyrics/get?title=Bohemian%20Rhapsody&artist=Queen&isrc=GBUM71029604",
    "lrcred": "https://lrc.red/s/GBUM71029604.ttml",
    "lrclib": "https://lrclib.net/api/get?artist_name=Queen&track_name=Bohemian%20Rhapsody",
    "spotify": "https://spclient.wg.spotify.com/color-lyrics/v2/track/0UHB9METy4VCXNgkcGqHqS?format=json&market=from_token",
    "binilyrics": "https://lyrics-api.binimum.org/?track=Bohemian%20Rhapsody&artist=Queen&duration=355",
    # Unison answers 404 for anything it has no lyrics for, and it has few:
    # this is a track it does have.
    "unison": "https://unison.boidu.dev/lyrics?song=Someone%20Like%20You&artist=Adele&duration=285",
    # "Tum Hi Ho" (id aRZbUYD7): one of the has_lyrics:true results, so this
    # actually exercises the lyrics.getLyrics call, not just search.
    "jiosaavn": "https://www.jiosaavn.com/api.php?__call=lyrics.getLyrics&_format=json&_marker=0&api_version=4&ctx=web6dot0&lyrics_id=aRZbUYD7",
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
    """Check direct lyrics server reachability for configured services."""
    del include_all_endpoints

    from . import get_amazon_endpoint

    servers = dict(_SERVERS)
    amazon_url = get_amazon_endpoint("spotbye1")
    if amazon_url:
        servers["amazon"] = f"{amazon_url.rstrip('/')}/lyrics/QM5FT1600115"

    if services:
        servers = {name: url for name, url in servers.items() if name in services}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            *(_probe(client, name, url) for name, url in servers.items())
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

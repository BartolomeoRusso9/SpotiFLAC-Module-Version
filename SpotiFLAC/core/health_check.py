from __future__ import annotations

import asyncio
import json
import time
from typing import NamedTuple
from urllib.parse import urlparse

import httpx

from ..core.endpoints import get_health_zarz_url

# ---------------------------------------------------------------------------
# Helper for payload validation
# ---------------------------------------------------------------------------


def _is_streaming_url(raw: str) -> bool:
    """Check if a string is a valid HTTP/HTTPS URL."""
    if not raw or not isinstance(raw, str):
        return False
    parsed = urlparse(raw.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _contains_streaming_url(body: str) -> bool:
    """Search for a valid streaming URL in the response text or JSON."""
    if not body.strip():
        return False
    if _is_streaming_url(body):
        return True
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            if "url" in data and _is_streaming_url(data["url"]):
                return True
            if "data" in data and isinstance(data["data"], dict):
                if "url" in data["data"] and _is_streaming_url(data["data"]["url"]):
                    return True
    except ValueError:
        pass
    return False


# ---------------------------------------------------------------------------
# Import endpoint lists directly from centralized provider
# ---------------------------------------------------------------------------

_TIDAL_MAX_MIRRORS = 8


def _load_endpoints() -> dict[str, list[tuple[str, str]]]:
    """
    Build the provider endpoint map used for health checks.

    The centralized extensions service is excluded from provider endpoints and checked separately.

    Returns:
        dict[str, list[tuple[str, str]]]: A mapping of provider names to method and URL pairs.
    """
    from ..core.endpoints import (
        get_qobuz_endpoints,
        get_tidal_post_endpoints,
        get_deezer_endpoint,
        get_amazon_endpoint,
        get_asian_provider_endpoint,
        get_soundcloud_cobalt,
        get_pandora_base_and_path,
    )

    endpoints: dict[str, list[tuple[str, str]]] = {}

    # ── Tidal ──────────────────────────────────────────────────────────────
    tidal_eps = []
    try:
        from ..providers.tidal import get_tidal_api_list

        for url in get_tidal_api_list()[:_TIDAL_MAX_MIRRORS]:
            tidal_eps.append(
                ("GET", f"{url.rstrip('/')}/track/?id=251380837&quality=LOSSLESS")
            )
    except Exception:
        pass

    for url in get_tidal_post_endpoints():
        tidal_eps.append(("POST", url))
    endpoints["tidal"] = tidal_eps

    # ── Qobuz ──────────────────────────────────────────────────────────────
    qobuz_eps = []
    _QOBUZ_PROBE_ID = "3135556"

    for url in get_qobuz_endpoints("stream"):
        sep = "" if url.endswith("=") else "?"
        qobuz_eps.append(("GET", f"{url}{_QOBUZ_PROBE_ID}{sep}quality=6"))

    for url in get_qobuz_endpoints("dl"):
        qobuz_eps.append(("GET", f"{url}track_id={_QOBUZ_PROBE_ID}&quality=6"))

    for url in get_qobuz_endpoints("post"):
        qobuz_eps.append(("POST", url))

    for url in get_qobuz_endpoints("flacdownloader"):
        qobuz_eps.append(("GET", f"{url.rstrip('/')}/prepare"))

    endpoints["qobuz"] = qobuz_eps

    # ── Deezer ─────────────────────────────────────────────────────────────
    dzr_res = get_deezer_endpoint("resolver")
    dzr_flac = get_deezer_endpoint("flacdownloader_prepare")

    deezer_eps = []
    if dzr_res:
        deezer_eps.append(("POST", dzr_res))
    if dzr_flac:
        deezer_eps.append(("GET", dzr_flac))
    endpoints["deezer"] = deezer_eps

    # ── Amazon ─────────────────────────────────────────────────────────────
    amazon_eps = []
    for key, method in [
        ("spotbye1", "POST"),
        ("spotbye2", "GET"),
        ("musicdl", "POST"),
        ("squid", "GET"),
    ]:
        url = get_amazon_endpoint(key)
        if url:
            amazon_eps.append((method, url))

    endpoints["amazon"] = amazon_eps

    # ── Apple Music ────────────────────────────────────────────────────────
    endpoints["apple"] = []

    # ── SoundCloud ─────────────────────────────────────────────────────────
    sc_cobalt = get_soundcloud_cobalt()
    endpoints["soundcloud"] = [("POST", sc_cobalt)] if sc_cobalt else []

    # ── YouTube ────────────────────────────────────────────────────────────
    endpoints["youtube"] = []

    # ── Pandora ────────────────────────────────────────────────────────────
    pan_base, pan_path = get_pandora_base_and_path()
    endpoints["pandora"] = []
    if pan_base and pan_path:
        endpoints["pandora"].append(("POST", f"{pan_base}{pan_path}"))

    # ── GD Studio API (Netease, Kuwo, Migu, Joox) ──────────────────────────
    for provider in ["netease", "kuwo", "migu", "joox"]:
        prov_eps = []
        gd_url = get_asian_provider_endpoint(provider, "gdstudio")
        if gd_url:
            prov_eps.append(("GET", gd_url))

        wjhe_url = get_asian_provider_endpoint(
            provider, "wjhe"
        ) or get_asian_provider_endpoint("joox", "wjhe")
        if wjhe_url:
            if "?" not in wjhe_url:
                wjhe_url = (
                    f"{wjhe_url.rstrip('/')}/url?ID=11259&quality=1000&format=flac"
                )
            prov_eps.append(("GET", wjhe_url))

        endpoints[provider] = prov_eps

    return endpoints


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UA = "SpotiFLAC-HealthCheck/4.5.0"
_TIMEOUT = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
_MAX_CONCURRENT = 25
_ZARZ_HEALTH_URL = get_health_zarz_url() or "https://api.zarz.moe/v1/health"
_GLOBAL_HC_TIMEOUT = 10  # secondi

# Carica gli endpoint una sola volta al momento dell'import
_ENDPOINTS: dict[str, list[tuple[str, str]]] = _load_endpoints()


def _make_async_client() -> httpx.AsyncClient:
    """
    Crea un AsyncClient con i limiti di connessione appropriati.
    Usato como context manager in run_health_check per garantire cleanup corretto.
    Una nuova istanza per ogni chiamata evita problemi di binding all'event loop.
    """
    return httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=_MAX_CONCURRENT,
            max_keepalive_connections=5,
            keepalive_expiry=10,
        ),
        timeout=_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class HealthResult(NamedTuple):
    provider: str
    url: str
    method: str
    ok: bool
    latency: float
    detail: str


# ---------------------------------------------------------------------------
# Core async check logic
# ---------------------------------------------------------------------------


async def _check_one(
    client: httpx.AsyncClient,
    provider: str,
    method: str,
    url: str,
) -> HealthResult:
    """
    Probe an endpoint and classify its health based on the response.

    Parameters:
        client (httpx.AsyncClient): HTTP client used to send the request.
        provider (str): Provider associated with the endpoint.
        method (str): HTTP method to use.
        url (str): Endpoint URL to probe.

    Returns:
        HealthResult: The provider, endpoint, request method, health status, latency in milliseconds, and response detail.
    """
    try:
        t0 = time.perf_counter()

        # Header di base
        req_kwargs: dict = {"headers": {"User-Agent": _UA}}

        # Iniezione degli header richiesti per endpoint di tipo FlacDownloader
        if "/prepare" in url:
            parsed = urlparse(url)
            origin = (
                f"{parsed.scheme}://{parsed.netloc}"
                if parsed.scheme and parsed.netloc
                else ""
            )
            req_kwargs["headers"].update(
                {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                    "Referer": f"{origin}/it/download" if origin else "",
                }
            )

        # Payload standard per l'API di Deezer
        if method == "POST" and provider == "deezer":
            req_kwargs["json"] = {
                "platform": "deezer",
                "url": "https://www.deezer.com/track/3135556",
            }

        resp = await client.request(method, url, follow_redirects=True, **req_kwargs)
        ms = (time.perf_counter() - t0) * 1000

        ok = False
        detail = f"HTTP {resp.status_code}"

        # ── POST probe ─────────────────────────────────────────────────────
        _is_post_probe = method == "POST" and "health" not in url
        if _is_post_probe:
            if resp.status_code == 200:
                body = resp.text
                if _contains_streaming_url(body):
                    ok, detail = True, "Stream OK"
                else:
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict) and (
                            data.get("error")
                            or data.get("status") == "error"
                            or data.get("success") is False
                        ):
                            ok = False
                            detail = str(
                                data.get("message") or data.get("error") or "API Error"
                            )[:10]
                        else:
                            ok, detail = True, "HTTP 200 OK"
                    except ValueError:
                        ok, detail = True, "HTTP 200 OK"

            elif resp.status_code >= 500:
                ok = False  # detail already set above ("HTTP 5xx")

            elif resp.status_code == 401:
                ok, detail = False, "Auth required"
                try:
                    data = json.loads(resp.text)
                    if isinstance(data, dict):
                        detail = str(
                            data.get("detail") or data.get("message") or "Auth required"
                        )[:10]
                except ValueError:
                    pass

            else:
                # Any other status (4xx other than 401, 3xx already followed) →
                # the server is reachable
                ok = True

            return HealthResult(provider, url, method, ok, ms, detail)

        # ── GET probes ─────────────────────────────────────────────────────
        if resp.status_code == 200:
            body = resp.text

            # ── Pandora / Tidal / Amazon ───────────────────────────────────
            if provider in ("pandora", "tidal", "amazon"):
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

            # ── Qobuz ──────────────────────────────────────────────────────
            elif provider in ("qobuz", "qbz"):
                if _contains_streaming_url(body):
                    ok = True
                else:
                    try:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict) and body.strip():
                            if parsed.get("t"):
                                ok, detail = True, "FlacDL OK"
                            else:
                                ok, detail = True, parsed.get("error", "JSON OK")
                    except ValueError:
                        detail = "No Stream URL"

            # ── Deezer ─────────────────────────────────────────────────────
            elif provider == "deezer":
                try:
                    parsed = json.loads(body)
                    if parsed.get("id") and not parsed.get("error"):
                        ok, detail = True, "API OK"
                    elif parsed.get("t"):
                        ok, detail = True, "FlacDL OK"
                    else:
                        detail = parsed.get("error", {}).get("message", "API Error")
                except ValueError:
                    detail = "Bad JSON"

            # ── Apple / SoundCloud / YouTube / default ─────────────────────
            else:
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

        elif resp.status_code in (404, 400):
            if provider in ("tidal", "qobuz", "qbz"):
                ok, detail = True, f"HTTP {resp.status_code} (Reachable)"

        elif resp.status_code == 401:
            ok, detail = False, "Auth required"
            try:
                parsed = json.loads(resp.text)
                if isinstance(parsed, dict):
                    detail = str(
                        parsed.get("detail") or parsed.get("message") or "Auth required"
                    )[:20]
            except ValueError:
                pass

        return HealthResult(provider, url, method, ok, ms, detail)

    except httpx.TimeoutException:
        return HealthResult(provider, url, method, False, -1, "timeout")
    except httpx.ConnectError:
        return HealthResult(provider, url, method, False, -1, "conn refused")
    except httpx.RequestError:
        return HealthResult(provider, url, method, False, -1, "req error")
    except Exception as exc:
        return HealthResult(provider, url, method, False, -1, str(exc)[:40])


async def _check_one_gated(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    provider: str,
    method: str,
    url: str,
) -> HealthResult:
    """
    Checks an endpoint while respecting the shared concurrency limit.

    Parameters:
        sem (asyncio.Semaphore): Semaphore controlling concurrent checks.
        provider (str): Provider associated with the endpoint.
        method (str): HTTP method used for the request.
        url (str): Endpoint URL to check.

    Returns:
        HealthResult: Result of the endpoint health check.
    """
    async with sem:
        return await _check_one(client, provider, method, url)


async def check_extensions_health(
    client: httpx.AsyncClient | None = None,
) -> HealthResult:
    """
    Check the health of the centralized extensions service.

    Parameters:
        client (httpx.AsyncClient | None): Optional HTTP client to use for the request.

    Returns:
        HealthResult: The extensions service health result, marked successful for an HTTP 200 response.
    """
    own_client = client is None
    if own_client:
        client = _make_async_client()
    try:
        t0 = time.perf_counter()
        resp = await client.get(_ZARZ_HEALTH_URL, headers={"User-Agent": _UA})
        ms = (time.perf_counter() - t0) * 1000

        if resp.status_code == 200:
            return HealthResult("extensions", _ZARZ_HEALTH_URL, "GET", True, ms, "ok")
        return HealthResult(
            "extensions", _ZARZ_HEALTH_URL, "GET", False, ms, f"HTTP {resp.status_code}"
        )
    except httpx.TimeoutException:
        return HealthResult("extensions", _ZARZ_HEALTH_URL, "GET", False, -1, "timeout")
    except httpx.RequestError as exc:
        return HealthResult(
            "extensions", _ZARZ_HEALTH_URL, "GET", False, -1, str(exc)[:40]
        )
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_health_check(
    services: list[str],
    *,
    include_all_endpoints: bool = True,
) -> list[HealthResult]:
    """
    Check the reachability of the requested providers and their configured endpoints.

    Parameters:
        services (list[str]): Provider names to check.
        include_all_endpoints (bool): Whether to check every configured endpoint or only the first endpoint for each provider.

    Returns:
        list[HealthResult]: Endpoint health results, including a local success result for YouTube.
    """
    results: list[HealthResult] = []
    task_list: list[tuple[str, str, str]] = []

    for svc in services:
        if svc == "youtube":
            results.append(
                HealthResult(
                    "youtube", "yt-dlp (local binary)", "CLI", True, 0.0, "local"
                )
            )

    remaining = [s for s in services if s != "youtube"]
    if not remaining:
        return results

    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async with _make_async_client() as client:
        for svc in remaining:
            eps = _ENDPOINTS.get(svc)
            if not eps:
                continue

            if include_all_endpoints:
                task_list.extend((svc, m, u) for m, u in eps)
            else:
                m, u = eps[0]
                task_list.append((svc, m, u))

        if task_list:
            task_map: dict[asyncio.Task[HealthResult], tuple[str, str, str]] = {
                asyncio.create_task(
                    _check_one_gated(sem, client, p, m, u),
                    name=f"hc-{p}-{m}",
                ): (p, m, u)
                for p, m, u in task_list
            }

            done, pending = await asyncio.wait(
                task_map.keys(),
                timeout=_GLOBAL_HC_TIMEOUT,
            )

            for task in done:
                try:
                    results.append(task.result())
                except Exception:
                    p, m, u = task_map[task]
                    results.append(HealthResult(p, u, m, False, -1, "task error"))

            for task in pending:
                p, m, u = task_map[task]
                task.cancel()
                results.append(HealthResult(p, u, m, False, -1, "global timeout"))

            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    auth_blocked = {
        r.provider for r in results if "auth" in r.detail.lower() and not r.ok
    }
    if auth_blocked:
        results = [
            (
                r._replace(ok=False, detail="Auth required")
                if r.provider in auth_blocked
                else r
            )
            for r in results
        ]

    svc_order = {svc: i for i, svc in enumerate(services)}
    results.sort(key=lambda r: (svc_order.get(r.provider, 99), str(r.url)))
    return results


async def run_health_check_with_extensions(
    services: list[str],
    *,
    include_all_endpoints: bool = True,
) -> tuple[list[HealthResult], HealthResult]:
    """
    Run provider endpoint checks and the extensions service health check concurrently.

    Parameters:
        services (list[str]): Providers whose endpoints should be checked.
        include_all_endpoints (bool): Whether to check every configured endpoint for each provider.

    Returns:
        tuple[list[HealthResult], HealthResult]: Provider check results and the extensions service result.
    """
    provider_results, ext_result = await asyncio.gather(
        run_health_check(services, include_all_endpoints=include_all_endpoints),
        check_extensions_health(),
    )
    return provider_results, ext_result


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_URL_MAX = 48


def print_health_report(
    results: list[HealthResult],
    *,
    show_urls: bool = True,
    extensions: HealthResult | None = None,
) -> None:
    """
    Print a table-formatted health report for provider endpoints.

    Parameters:
        results (list[HealthResult]): Endpoint health-check results to display.
        show_urls (bool): Whether to include endpoint URLs in the report.
        extensions (HealthResult | None): Optional health result for the extensions service.
    """
    if not results:
        print("  Nessun provider da verificare.")
        return

    url_col = _URL_MAX if show_urls else 0
    header_top = "┬".join(
        ["─" * 14, "─" * 6, "─" * 12, "─" * 9]
        + (["─" * (url_col + 2)] if show_urls else [])
    )
    header_bot = "┼".join(
        ["─" * 14, "─" * 6, "─" * 12, "─" * 9]
        + (["─" * (url_col + 2)] if show_urls else [])
    )

    print()
    print(f"  ┌{header_top}┐")
    hdr = f"  │ {'Provider':<12} │ {'M':<4} │ {'Status':<10} │ {'Latency':>7} │"
    if show_urls:
        hdr += f" {'Endpoint':<{url_col}} │"
    print(hdr)
    print(f"  ├{header_bot}┤")

    prev_provider = None
    for r in results:
        symbol = "✅" if r.ok else "❌"
        lat_str = f"{r.latency:>5.0f} ms" if r.latency >= 0 else "  timeout"
        detail = r.detail[:10]

        provider_cell = r.provider if r.provider != prev_provider else ""
        prev_provider = r.provider

        row = f"  │ {provider_cell:<12} │ {r.method:<4} │ {symbol} {detail:<8} │ {lat_str:>7} │"
        if show_urls:
            short_url = r.url[-url_col:] if len(r.url) > url_col else r.url
            row += f" {short_url:<{url_col}} │"
        print(row)

    footer = "┴".join(
        ["─" * 14, "─" * 6, "─" * 12, "─" * 9]
        + (["─" * (url_col + 2)] if show_urls else [])
    )
    print(f"  └{footer}┘")

    ok_count = sum(1 for r in results if r.ok)
    prov_ok = len({r.provider for r in results if r.ok})
    prov_total = len({r.provider for r in results})
    print(
        f"\n  {ok_count}/{len(results)} endpoints reachable "
        f"({prov_ok}/{prov_total} providers with at least one working endpoint).\n"
    )

    if extensions is not None:
        _print_extensions_section(extensions)


def _print_extensions_section(extensions: HealthResult) -> None:
    """
    Print the health status of the extensions service.
    """
    symbol = "✅" if extensions.ok else "❌"
    lat_str = f"{extensions.latency:>5.0f} ms" if extensions.latency >= 0 else "timeout"

    width = 14 + 3 + 6 + 3 + 12 + 3 + 9 + 6
    print(f"  ┌{'─' * width}┐")
    print(f"  │ Estensioni{' ' * (width - 11)}│")
    print(f"  ├{'─' * width}┤")
    row = f"  │ {symbol} {extensions.detail:<10} {lat_str:>10}"
    print(f"{row}{' ' * max(0, width - len(row) + 4)}│")
    print(f"  └{'─' * width}┘")
    print(f"      {extensions.url}\n")


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def any_service_ok(results: list[HealthResult]) -> bool:
    """True if at least one endpoint for at least one provider is reachable."""
    return any(r.ok for r in results)


def provider_ok(results: list[HealthResult], provider: str) -> bool:
    """True if at least one endpoint for the indicated provider is reachable."""
    return any(r.ok for r in results if r.provider == provider)


def get_working_providers(results: list[HealthResult]) -> list[str]:
    """Returns the list of providers with at least one working endpoint."""
    return list(dict.fromkeys(r.provider for r in results if r.ok))

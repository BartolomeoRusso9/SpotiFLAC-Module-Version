"""
Service health check — verifica la disponibilità dei provider prima del download.
Importa gli endpoint direttamente dai moduli provider invece di duplicarli.
Esegue richieste parallele con timeout breve (5 s) agli endpoint reali.

Uso:
    results = run_health_check(["tidal", "qobuz", "deezer"])
    print_health_report(results)
    all_ok = any(r.ok for r in results)
"""
from __future__ import annotations

import concurrent.futures
import time
import json
from typing import NamedTuple
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Helper per la validazione del payload
# ---------------------------------------------------------------------------

def _is_streaming_url(raw: str) -> bool:
    """Verifica se una stringa è un URL HTTP/HTTPS valido."""
    if not raw or not isinstance(raw, str):
        return False
    parsed = urlparse(raw.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def _contains_streaming_url(body: str) -> bool:
    """Cerca un URL di streaming valido nel testo o nel JSON della risposta."""
    if not body.strip():
        return False

    # 1. Controlla se il body stesso è un URL diretto
    if _is_streaming_url(body):
        return True

    # 2. Cerca nel JSON (formato diretto o annidato in "data")
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            # Formato {"url": "..."}
            if "url" in data and _is_streaming_url(data["url"]):
                return True
            # Formato {"data": {"url": "..."}}
            if "data" in data and isinstance(data["data"], dict):
                if "url" in data["data"] and _is_streaming_url(data["data"]["url"]):
                    return True
    except ValueError:
        pass

    return False

# ---------------------------------------------------------------------------
# Import endpoint lists directly from provider modules
# ---------------------------------------------------------------------------

def _load_endpoints() -> dict[str, list[tuple[str, str]]]:
    """
    Carica dinamicamente gli endpoint da ogni modulo provider.
    Ritorna un dict {provider_name: [(method, url), ...]}
    """
    endpoints: dict[str, list[tuple[str, str]]] = {}

    # ── Tidal ──────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.tidal import (
            _TIDAL_APIS_GET,
            _TIDAL_API_POST,
            get_tidal_api_list,
        )
        try:
            tidal_get = get_tidal_api_list()
        except Exception:
            tidal_get = list(_TIDAL_APIS_GET)

        tidal_eps = [("GET", f"{url.rstrip('/')}/track/?id=1&quality=LOSSLESS")
                     for url in tidal_get]
        tidal_eps += [("POST", url) for url in _TIDAL_API_POST]
        tidal_eps.append(("GET", "https://api.zarz.moe/v1/health"))

        endpoints["tidal"] = tidal_eps
    except ImportError:
        endpoints["tidal"] = [
            ("GET", "https://api.zarz.moe/v1/health")
        ]

    # ── Qobuz ──────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.qobuz import _STREAM_APIS, _API_BASE
        qobuz_eps = [("GET", f"{url}1&quality=6") if url.endswith("=")
                     else ("GET", f"{url}1?quality=6")
                     for url in _STREAM_APIS]
        qobuz_eps.append(("GET", f"{_API_BASE}/track/search?query=test&limit=1&app_id=0"))
        qobuz_eps.append(("GET", "https://api.zarz.moe/v1/health"))

        endpoints["qobuz"] = qobuz_eps
    except ImportError:
        endpoints["qobuz"] = [
            ("GET", "https://api.zarz.moe/v1/health")
        ]

    # ── Deezer ─────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.deezer import _RESOLVER_URL
        endpoints["deezer"] = [
            ("POST", _RESOLVER_URL),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]
    except ImportError:
        endpoints["deezer"] = [
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── Amazon ─────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.amazon import API_ENDPOINTS
        amazon_list = []
        for val in API_ENDPOINTS.values():
            if isinstance(val, str):
                amazon_list.append(("GET", val))
            elif isinstance(val, dict):
                for sub_val in val.values():
                    if isinstance(sub_val, str):
                        amazon_list.append(("GET", sub_val))

        endpoints["amazon"] = amazon_list
        endpoints["amazon"].append(("GET", "https://api.zarz.moe/v1/health"))
    except ImportError:
        endpoints["amazon"] = [
            ("GET", "https://api.zarz.moe/v1/health")
        ]

    # ── Apple Music ────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.apple_music import API_ENDPOINTS as APPLE_DL_ENDPOINTS
        try:
            from SpotiFLAC.providers.apple_music_metadata import API_ENDPOINTS as APPLE_META_ENDPOINTS
        except ImportError:
            APPLE_META_ENDPOINTS = {}

        endpoints["apple"] = [
            ("POST", APPLE_DL_ENDPOINTS.get("proxy_direct", "https://api.zarz.moe/v1/dl/app2")),
            ("GET",  f"{APPLE_DL_ENDPOINTS.get('proxy_queued', 'https://api.zarz.moe/v1/dl/app')}/status/test"),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]
    except ImportError:
        endpoints["apple"] = [
            ("POST", "https://api.zarz.moe/v1/dl/app2"),
            ("GET",  "https://api.zarz.moe/v1/dl/app/status/test"),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── SoundCloud ─────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.soundcloud import SoundCloudProvider
        sc = SoundCloudProvider.__new__(SoundCloudProvider)
        sc_api = getattr(sc, "api_url", "https://api-v2.soundcloud.com")
        cobalt = getattr(sc, "cobalt_api", "https://api.zarz.moe/v1/dl/cobalt/")
        endpoints["soundcloud"] = [
            ("GET",  sc_api),
            ("POST", cobalt),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]
    except Exception:
        endpoints["soundcloud"] = [
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── YouTube ────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.youtube import COBALT_API_URL
        endpoints["youtube"] = [
            ("POST", COBALT_API_URL),
        ]
    except ImportError:
        endpoints["youtube"] = []

    # ── Pandora ────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.pandora import _API_BASE_URL, _DOWNLOAD_PATH
        endpoints["pandora"] = [
            ("GET",  f"{_API_BASE_URL}/v1/health"),
            ("POST", f"{_API_BASE_URL}{_DOWNLOAD_PATH}"),
        ]
    except ImportError:
        endpoints["pandora"] = [
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── SpotiDownloader ────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.spotidownloader import _API_BASE as SPOTI_API_BASE
        endpoints["spoti"] = [("GET", SPOTI_API_BASE)]
    except ImportError:
        endpoints["spoti"] = [("GET", "https://api.spotidownloader.com/")]

    return endpoints

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UA      = "SpotiFLAC-HealthCheck/1.0"
_TIMEOUT = 5

# Carica gli endpoint una sola volta al momento dell'import
_ENDPOINTS: dict[str, list[tuple[str, str]]] = _load_endpoints()

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class HealthResult(NamedTuple):
    provider: str
    url:      str
    method:   str
    ok:       bool
    latency:  float   # ms; -1 = irraggiungibile
    detail:   str

# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

def _check_one(provider: str, method: str, url: str) -> HealthResult:
    try:
        t0 = time.perf_counter()
        resp = requests.request(
            method, url,
            headers         = {"User-Agent": _UA},
            timeout         = _TIMEOUT,
            allow_redirects = True,
        )
        ms = (time.perf_counter() - t0) * 1000

        ok = False
        detail = f"HTTP {resp.status_code}"

        if resp.status_code == 200:
            body = resp.text

            # ── Centralised Zarz health check ──────────────────────────────
            if "api.zarz.moe/v1/health" in url:
                try:
                    data = json.loads(body)
                    services = data.get("services", {})
                    svc_key = "qobuz" if provider == "qbz" else provider

                    if svc_key in services:
                        svc_info = services[svc_key]
                        if svc_info.get("status") == 200:
                            ok = True
                            detail = svc_info.get("detail") or "ok"
                        else:
                            ok = False
                            inner_detail = svc_info.get("detail") or "error"
                            detail = f"Zarz {svc_info.get('status')} ({inner_detail})"
                    else:
                        ok = True
                        detail = "Zarz Link OK"
                except ValueError:
                    detail = "Bad Health Payload"

                return HealthResult(provider, url, method, ok, ms, detail)

            # ── Pandora-specific checks ────────────────────────────────────
            if provider == "pandora":
                # The Pandora download endpoint returns a JSON body when reachable
                # (even a 422/400 means the service is up); a non-empty body is enough.
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

            elif provider == "amazon":
                if '"amazonMusic":"up"' in body:
                    ok = True
                else:
                    detail = "Bad Payload"

            elif provider == "tidal":
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

            elif provider in ("qobuz", "qbz"):
                if _contains_streaming_url(body):
                    ok = True
                else:
                    detail = "No Stream URL"

            elif provider == "spoti":
                try:
                    json.loads(body)
                    ok = True
                except ValueError:
                    detail = "HTML/CF Block"

            elif provider in ("deezer", "apple", "soundcloud", "youtube"):
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

            else:
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

        return HealthResult(provider, url, method, ok, ms, detail)

    except requests.Timeout:
        return HealthResult(provider, url, method, False, -1, "timeout")
    except requests.ConnectionError:
        return HealthResult(provider, url, method, False, -1, "conn refused")
    except Exception as exc:
        return HealthResult(provider, url, method, False, -1, str(exc)[:40])


def run_health_check(
        services: list[str],
        *,
        include_all_endpoints: bool = True,
) -> list[HealthResult]:
    """
    Controlla i provider richiesti in parallelo.
    """
    tasks: list[tuple[str, str, str]] = []
    results: list[HealthResult] = []

    for svc in services:
        if svc == "youtube":
            results.append(HealthResult("youtube", "yt-dlp (local binary)", "CLI", True, 0.0, "local"))

        eps = _ENDPOINTS.get(svc)
        if not eps:
            continue
        if include_all_endpoints:
            tasks.extend((svc, m, u) for m, u in eps)
        else:
            m, u = eps[0]
            tasks.append((svc, m, u))

    if not tasks:
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks), 20)) as pool:
        futs = {pool.submit(_check_one, p, m, u): (p, m, u) for p, m, u in tasks}
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    svc_order = {svc: i for i, svc in enumerate(services)}
    results.sort(key=lambda r: (svc_order.get(r.provider, 99), str(r.url)))
    return results

# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_URL_MAX = 48

def print_health_report(
        results: list[HealthResult],
        *,
        show_urls: bool = True,
) -> None:
    """Stampa un report formattato a tabella dei risultati."""
    if not results:
        print("  Nessun provider da verificare.")
        return

    url_col = _URL_MAX if show_urls else 0
    total_w = 14 + 6 + 12 + 9 + (url_col + 3 if show_urls else 0)

    sep_inner = "─" * total_w
    sep_mid   = "┼".join(["─" * 14, "─" * 6, "─" * 12, "─" * 9] +
                         (["─" * (url_col + 2)] if show_urls else []))
    header_top = "┬".join(["─" * 14, "─" * 6, "─" * 12, "─" * 9] +
                          (["─" * (url_col + 2)] if show_urls else []))
    header_bot = "┼".join(["─" * 14, "─" * 6, "─" * 12, "─" * 9] +
                          (["─" * (url_col + 2)] if show_urls else []))

    print()
    print(f"  ┌{header_top}┐")
    hdr = f"  │ {'Provider':<12} │ {'M':<4} │ {'Status':<10} │ {'Latency':>7} │"
    if show_urls:
        hdr += f" {'Endpoint':<{url_col}} │"
    print(hdr)
    print(f"  ├{header_bot}┤")

    prev_provider = None
    for r in results:
        symbol  = "✅" if r.ok else "❌"
        lat_str = f"{r.latency:>5.0f} ms" if r.latency >= 0 else "  timeout"
        detail  = r.detail[:10]

        provider_cell = r.provider if r.provider != prev_provider else ""
        prev_provider = r.provider

        row = (f"  │ {provider_cell:<12} │ {r.method:<4} │ {symbol} {detail:<8} │ {lat_str:>7} │")
        if show_urls:
            short_url = r.url[-url_col:] if len(r.url) > url_col else r.url
            row += f" {short_url:<{url_col}} │"
        print(row)

    print(f"  └{'┴'.join(['─'*14,'─'*6,'─'*12,'─'*9] + (['─'*(url_col+2)] if show_urls else []))}┘")

    ok_count    = sum(1 for r in results if r.ok)
    prov_ok     = len({r.provider for r in results if r.ok})
    prov_total  = len({r.provider for r in results})
    print(f"\n  {ok_count}/{len(results)} endpoints reachable "
          f"({prov_ok}/{prov_total} providers with at least one working endpoint).\n")


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def any_service_ok(results: list[HealthResult]) -> bool:
    """True se almeno un endpoint di almeno un provider è raggiungibile."""
    return any(r.ok for r in results)


def provider_ok(results: list[HealthResult], provider: str) -> bool:
    """True se almeno un endpoint del provider indicato è raggiungibile."""
    return any(r.ok for r in results if r.provider == provider)


def get_working_providers(results: list[HealthResult]) -> list[str]:
    """Ritorna la lista dei provider con almeno un endpoint funzionante."""
    return list(dict.fromkeys(r.provider for r in results if r.ok))
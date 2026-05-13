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
from typing import NamedTuple

import requests

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
        # Prova prima la lista cached (include eventuali URL dal gist)
        try:
            tidal_get = get_tidal_api_list()
        except Exception:
            tidal_get = list(_TIDAL_APIS_GET)

        tidal_eps = [("GET", f"{url.rstrip('/')}/track/?id=1&quality=LOSSLESS")
                     for url in tidal_get]
        tidal_eps += [("POST", url) for url in _TIDAL_API_POST]
        endpoints["tidal"] = tidal_eps
    except ImportError:
        endpoints["tidal"] = [("GET", "https://eu-central.monochrome.tf/track/?id=1&quality=LOSSLESS")]

    # ── Qobuz ──────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.qobuz import _STREAM_APIS, _API_BASE
        qobuz_eps = [("GET", f"{url}1&quality=6") if url.endswith("=")
                     else ("GET", f"{url}1?quality=6")
                     for url in _STREAM_APIS]
        # Aggiunge anche l'API ufficiale Qobuz (search pubblica)
        qobuz_eps.append(("GET", f"{_API_BASE}/track/search?query=test&limit=1&app_id=0"))
        endpoints["qobuz"] = qobuz_eps
    except ImportError:
        endpoints["qobuz"] = [("GET", "https://dab.yeet.su/api/stream?trackId=1&quality=6")]

    # ── Deezer ─────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.deezer import _RESOLVER_URL
        endpoints["deezer"] = [
            ("GET",  "https://api.deezer.com/2.0/track/isrc:USUM71703861"),
            ("POST", _RESOLVER_URL),
        ]
    except ImportError:
        endpoints["deezer"] = [
            ("GET", "https://api.deezer.com/2.0/track/isrc:USUM71703861"),
        ]

    # ── Amazon ─────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.amazon import API_ENDPOINTS
        # Estrae tutti gli URL dal dizionario e li prepara per il controllo "GET"
        endpoints["amazon"] = [("GET", url) for url in API_ENDPOINTS.values()]
    except ImportError:
        # Fallback manuale con i due endpoint in caso di problemi di importazione
        endpoints["amazon"] = [
            ("GET", "https://amazon.spotbye.qzz.io/api"),
            ("GET", "https://api.zarz.moe/v1/dl/amazeamazeamaze")
        ]


    # ── Apple Music ────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.apple_music import API_ENDPOINTS as APPLE_DL_ENDPOINTS
        try:
            from SpotiFLAC.providers.apple_music_metadata import API_ENDPOINTS as APPLE_META_ENDPOINTS
        except ImportError:
            # Fallback se non trova il modulo metadata
            APPLE_META_ENDPOINTS = {"itunes_search": "https://itunes.apple.com/search"}

        endpoints["apple"] = [
            ("GET",  f"{APPLE_META_ENDPOINTS.get('itunes_search', 'https://itunes.apple.com/search')}?term=test&limit=1"),
            ("POST", APPLE_DL_ENDPOINTS.get("proxy_direct", "https://api.zarz.moe/v1/dl/app2")),
            ("GET",  f"{APPLE_DL_ENDPOINTS.get('proxy_queued', 'https://api.zarz.moe/v1/dl/app')}/status/test"),
        ]
    except ImportError:
        endpoints["apple"] = [
            ("GET",  "https://itunes.apple.com/search?term=test&limit=1"),
            ("POST", "https://api.zarz.moe/v1/dl/app2"),
            ("GET",  "https://api.zarz.moe/v1/dl/app/status/test"),
        ]

    # ── SoundCloud ─────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.soundcloud import SoundCloudProvider
        sc = SoundCloudProvider.__new__(SoundCloudProvider)
        sc_api = getattr(sc, "api_url", "https://api-v2.soundcloud.com")
        cobalt = getattr(sc, "cobalt_api", "https://api.zarz.moe/v1/dl/cobalt/")
        endpoints["soundcloud"] = [
            ("GET",  "https://soundcloud.com/"),
            ("GET",  sc_api),
            ("POST", cobalt),
        ]
    except Exception:
        endpoints["soundcloud"] = [
            ("GET", "https://soundcloud.com/"),
            ("GET", "https://api-v2.soundcloud.com"),
        ]

    # ── YouTube ────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.youtube import COBALT_API_URL
        endpoints["youtube"] = [
            ("GET",  "https://music.youtube.com/"),
            ("POST", COBALT_API_URL),
        ]
    except ImportError:
        endpoints["youtube"] = [
            ("GET", "https://music.youtube.com/"),
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
        ok = resp.status_code < 500
        return HealthResult(provider, url, method, ok, ms, f"HTTP {resp.status_code}")
    except requests.Timeout:
        return HealthResult(provider, url, method, False, -1, "timeout")
    except requests.ConnectionError:
        return HealthResult(provider, url, method, False, -1, "connection refused")
    except Exception as exc:
        return HealthResult(provider, url, method, False, -1, str(exc)[:40])


def run_health_check(
        services: list[str],
        *,
        include_all_endpoints: bool = True,
) -> list[HealthResult]:
    """
    Controlla i provider richiesti in parallelo.

    Args:
        services:              Lista di nomi provider da verificare.
        include_all_endpoints: Se True, controlla tutti gli endpoint di ogni provider.
                               Se False (default), controlla solo il primo endpoint
                               per provider (più veloce, solo per sanity check).

    Ritorna i risultati ordinati per provider → endpoint.
    """
    tasks: list[tuple[str, str, str]] = []  # (provider, method, url)

    for svc in services:
        eps = _ENDPOINTS.get(svc)
        if not eps:
            continue
        if include_all_endpoints:
            tasks.extend((svc, m, u) for m, u in eps)
        else:
            # Solo il primo endpoint per provider (representative check)
            m, u = eps[0]
            tasks.append((svc, m, u))

    if not tasks:
        return []

    results: list[HealthResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks), 20)) as pool:
        futs = {pool.submit(_check_one, p, m, u): (p, m, u) for p, m, u in tasks}
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    # Ordina per provider (rispetta l'ordine di `services`), poi per URL
    svc_order = {svc: i for i, svc in enumerate(services)}
    results.sort(key=lambda r: (svc_order.get(r.provider, 99), r.url))
    return results


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_URL_MAX = 48   # max chars shown for endpoint URL in table


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

        # Raggruppa visivamente per provider
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


def print_endpoint_summary() -> None:
    """Stampa quanti endpoint sono configurati per ogni provider."""
    print("\n  Configured endpoints per provider:")
    for provider, eps in _ENDPOINTS.items():
        print(f"    {provider:<14} {len(eps):>2} endpoint(s)")
    print()


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
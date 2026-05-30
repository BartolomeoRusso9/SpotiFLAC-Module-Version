"""
Service health check — verifica la disponibilità dei provider prima del download.
Importa gli endpoint direttamente dai moduli provider invece di duplicarli.
Esegue richieste parallele con timeout breve (5 s) agli endpoint reali (non ufficiali).

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

import httpx
from SpotiFLAC.core.http import NetworkManager

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
    Esclude le API ufficiali, ma mantiene i check centralizzati Zarz.
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

        # Usiamo un ID reale per il test invece di 1
        tidal_eps = [("GET", f"{url.rstrip('/')}/track/?id=251380837&quality=LOSSLESS")
                     for url in tidal_get]
        # POST probe: body vuoto ma il server risponde comunque
        tidal_eps += [("POST", url) for url in _TIDAL_API_POST]
        tidal_eps.append(("GET", "https://api.zarz.moe/v1/health"))

        endpoints["tidal"] = tidal_eps
    except ImportError:
        endpoints["tidal"] = [
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── Qobuz ──────────────────────────────────────────────────────────────
    try:
        # Importiamo solo le API di download di terze parti, NON l'API ufficiale (_API_BASE)
        from SpotiFLAC.providers.qobuz import _STREAM_APIS, _POST_APIS
        qobuz_eps: list[tuple[str, str]] = []
        _QOBUZ_PROBE_ID = "3135556"
        
        # Sondiamo le API streaming (GET)
        for url in _STREAM_APIS:
            if url.endswith("="):
                qobuz_eps.append(("GET", f"{url}{_QOBUZ_PROBE_ID}&quality=6"))
            else:
                qobuz_eps.append(("GET", f"{url}{_QOBUZ_PROBE_ID}?quality=6"))
                
        # Sondiamo le API POST
        for url in _POST_APIS:
            qobuz_eps.append(("POST", url))

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
            ("POST", "https://api.zarz.moe/v1/dl/dzr"),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── Amazon ─────────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.amazon import API_ENDPOINTS
        amazon_list: list[tuple[str, str]] = []
        for val in API_ENDPOINTS.values():
            if isinstance(val, dict):
                base_url = val.get("base_url", "")
                if base_url:
                    amazon_list.append(("POST", base_url))
            elif isinstance(val, str):
                amazon_list.append(("POST", val))

        amazon_list.append(("GET", "https://api.zarz.moe/v1/health"))
        endpoints["amazon"] = amazon_list
    except ImportError:
        endpoints["amazon"] = [
            ("POST", "https://amz.spotbye.qzz.io/api"),
            ("POST", "https://amazon.spotbye.qzz.io/api"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]

    # ── Apple Music ────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.apple_music import API_ENDPOINTS as APPLE_DL_ENDPOINTS
        endpoints["apple"] = [
            ("POST", APPLE_DL_ENDPOINTS.get("proxy_direct", "https://api.zarz.moe/v1/dl/app2")),
            ("GET",  f"{APPLE_DL_ENDPOINTS.get('proxy_queued', 'https://api.zarz.moe/v1/dl/app')}/status/test"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]
    except ImportError:
        endpoints["apple"] = [
            ("POST", "https://api.zarz.moe/v1/dl/app2"),
            ("GET",  "https://api.zarz.moe/v1/dl/app/status/test"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]

    # ── SoundCloud ─────────────────────────────────────────────────────────
    try:
        from SpotiFLAC.providers.soundcloud import SoundCloudProvider
        sc = SoundCloudProvider.__new__(SoundCloudProvider)
        cobalt  = getattr(sc, "cobalt_api", "https://api.zarz.moe/v1/dl/cobalt/")
        endpoints["soundcloud"] = [
            ("POST", cobalt),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]
    except Exception:
        endpoints["soundcloud"] = [
            ("POST", "https://api.zarz.moe/v1/dl/cobalt/"),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── YouTube ────────────────────────────────────────────────────
    # YouTube usa yt-dlp locale, nessun endpoint HTTP da sondare.
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

# Endpoint POST-only che non richiedono payload per rispondere (accettano body vuoto)
_POST_PROBE_ONLY: frozenset[str] = frozenset()

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

        req_kwargs: dict = {
            "headers":         {"User-Agent": _UA},
            "timeout":         _TIMEOUT,
            "allow_redirects": True,
        }

        if method == "POST":
            if provider == "deezer":
                # Il resolver specifico di Deezer richiede un payload particolare
                req_kwargs["json"] = {
                    "platform": "deezer",
                    "url": "https://www.deezer.com/track/3135556"
                }
            else:
                test_urls = {
                    "apple": "https://music.apple.com/us/album/test/123456789?i=123456789",
                    "amazon": "https://music.amazon.com/albums/B000000000?trackAsin=B000000000",
                    "soundcloud": "https://soundcloud.com/spinninrecords/martin-garrix-animals",
                    "pandora": "https://pandora.com/artist/test/test/test",
                    "tidal": "https://tidal.com/browse/track/1"
                }
                dummy_url = test_urls.get(provider, "https://example.com/track/123")
                req_kwargs["json"] = {"url": dummy_url}

        # Usiamo il nostro connection pool centralizzato
        client = NetworkManager.get_sync_client()
        resp = client.request(method, url, follow_redirects=True, **{k: v for k, v in req_kwargs.items() if k != 'allow_redirects'})
        ms = (time.perf_counter() - t0) * 1000

        ok     = False
        detail = f"HTTP {resp.status_code}"

        # ── POST probe ─────────────────────────────────────────────────────
        _is_post_probe = (method == "POST" and "health" not in url)
        if _is_post_probe:
            if resp.status_code == 200:
                body = resp.text
                if _contains_streaming_url(body):
                    ok     = True
                    detail = "Stream OK"
                else:
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict) and (data.get("error") or data.get("status") == "error" or data.get("success") is False):
                            ok      = False
                            err_msg = data.get("message") or data.get("error") or "API Error"
                            detail  = str(err_msg)[:10]
                        else:
                            ok     = True
                            detail = "HTTP 200 OK"
                    except ValueError:
                        ok     = True
                        detail = "HTTP 200 OK"
            else:
                ok     = True  # Qualsiasi altro status code HTTP indica che il server è raggiungibile
                detail = f"HTTP {resp.status_code}"
                
                if resp.status_code >= 500:
                    ok = False
                elif resp.status_code == 401:
                    try:
                        data = json.loads(resp.text)
                        if isinstance(data, dict) and data.get("detail") == "auth_required":
                            ok = False
                            detail = "Auth required"
                    except ValueError:
                        pass
                
            return HealthResult(provider, url, method, ok, ms, detail)

        # ── GET probes ─────────────────────────────────────────────────────
        if resp.status_code == 200:
            body = resp.text

            # ── Centralised Zarz health check ──────────────────────────────
            if "api.zarz.moe/v1/health" in url or "/v1/health" in url:
                try:
                    data     = json.loads(body)
                    services = data.get("services", {})
                    svc_key  = "qobuz" if provider == "qbz" else provider

                    if svc_key in services:
                        svc_info = services[svc_key]
                        
                        # 1. Se Zarz ci segnala 401 auth_required, è sempre non raggiungibile
                        if svc_info.get("status") == 401 and svc_info.get("detail") == "auth_required":
                            ok = False
                            detail = "Auth required"
                            
                        # 2. Altrimenti ci fidiamo del flag "ok" fornito da Zarz, oppure di status 200
                        elif svc_info.get("ok") is True or svc_info.get("status") == 200:
                            ok = True
                            detail = svc_info.get("detail") or "ok"
                            
                        # 3. Altri errori (es. 500)
                        else:
                            ok = False
                            inner_detail = svc_info.get("detail") or "error"
                            detail = f"Zarz {svc_info.get('status')} ({inner_detail})"
                    else:
                        ok     = True
                        detail = "Zarz Link OK"
                except ValueError:
                    detail = "Bad Health Payload"

                return HealthResult(provider, url, method, ok, ms, detail)

            # ── Pandora ────────────────────────────────────────────────────
            if provider == "pandora":
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

            # ── Amazon ─────────────────────────────────────────────────────
            elif provider == "amazon":
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

            # ── Tidal ──────────────────────────────────────────────────────
            elif provider == "tidal":
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
                            ok     = True
                            detail = parsed.get("error", "JSON OK")
                    except ValueError:
                        detail = "No Stream URL"

            # ── SpotiDownloader ────────────────────────────────────────────
            elif provider == "spoti":
                try:
                    json.loads(body)
                    ok = True
                except ValueError:
                    detail = "HTML/CF Block"

            # ── Deezer ─────────────────────────────────────────────────────
            elif provider == "deezer":
                try:
                    parsed = json.loads(body)
                    if parsed.get("id") and not parsed.get("error"):
                        ok     = True
                        detail = "API OK"
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
        elif resp.status_code == 404 or resp.status_code == 400:
             # Se le API GET dei mirror ritornano un 404 su una track dummy, significa comunque che sono attive
             if provider in ("tidal", "qobuz", "qbz"):
                 ok = True
                 detail = f"HTTP {resp.status_code} (Reachable)"
        elif resp.status_code == 401:
            try:
                parsed = json.loads(body)
                if parsed.get("detail") == "auth_required":
                    detail = "Authentication required"
            except ValueError:
                detail = "Unknown error"

        return HealthResult(provider, url, method, ok, ms, detail)

    except httpx.TimeoutException:
        return HealthResult(provider, url, method, False, -1, "timeout")
    except httpx.ConnectError:
        return HealthResult(provider, url, method, False, -1, "conn refused")
    except httpx.RequestError as exc:
        return HealthResult(provider, url, method, False, -1, "req error")
    except Exception as exc:
        return HealthResult(provider, url, method, False, -1, str(exc)[:40])


def run_health_check(
        services: list[str],
        *,
        include_all_endpoints: bool = True,
) -> list[HealthResult]:
    tasks:   list[tuple[str, str, str]] = []
    results: list[HealthResult]         = []

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

    # ── Post-processing: auth_required dal Zarz health check blocca l'intero provider ──
    auth_blocked: set[str] = {
        r.provider for r in results
        if "auth" in r.detail.lower() and not r.ok
    }
    if auth_blocked:
        results = [
            r._replace(ok=False, detail="Auth required")
            if r.provider in auth_blocked
            else r
            for r in results
        ]

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

    url_col    = _URL_MAX if show_urls else 0
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

    ok_count   = sum(1 for r in results if r.ok)
    prov_ok    = len({r.provider for r in results if r.ok})
    prov_total = len({r.provider for r in results})
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
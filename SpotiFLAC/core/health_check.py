"""
Service health check — verifica la disponibilità dei provider prima del download.
Importa gli endpoint direttamente dai moduli provider invece di duplicarli.
Esegue richieste parallele con timeout breve (2 s) agli endpoint reali (non ufficiali).
Usa asyncio + httpx.AsyncClient al posto di thread per I/O non bloccante.

Uso (contesto async):
    results = await run_health_check(["tidal", "qobuz", "deezer"])

Uso (contesto sync):
    results = run_health_check_sync(["tidal", "qobuz", "deezer"])

    print_health_report(results)
    all_ok = any(r.ok for r in results)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import NamedTuple
from urllib.parse import urlparse

import httpx


# ---------------------------------------------------------------------------
# Helper per la validazione del payload  (invariato)
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
# Import endpoint lists directly from provider modules  (invariato)
# ---------------------------------------------------------------------------

_TIDAL_MAX_MIRRORS = 8


def _load_endpoints() -> dict[str, list[tuple[str, str]]]:
    """
    Carica dinamicamente gli endpoint da ogni modulo provider.
    Ritorna un dict {provider_name: [(method, url), ...]}
    Esclude le API ufficiali, ma mantiene i check centralizzati Zarz.
    """
    endpoints: dict[str, list[tuple[str, str]]] = {}

    # ── Tidal ──────────────────────────────────────────────────────────────
    try:
        from ..providers.tidal import (
            _TIDAL_APIS_GET,
            _TIDAL_API_POST,
            get_tidal_api_list,
        )
        try:
            tidal_get = get_tidal_api_list()
        except Exception:
            tidal_get = list(_TIDAL_APIS_GET)

        tidal_get = tidal_get[:_TIDAL_MAX_MIRRORS]
        tidal_eps = [
            ("GET", f"{url.rstrip('/')}/track/?id=251380837&quality=LOSSLESS")
            for url in tidal_get
        ]
        tidal_eps += [("POST", url) for url in _TIDAL_API_POST]
        tidal_eps.append(("GET", "https://api.zarz.moe/v1/health"))
        endpoints["tidal"] = tidal_eps
    except ImportError:
        endpoints["tidal"] = [("GET", "https://api.zarz.moe/v1/health")]

    # ── Qobuz ──────────────────────────────────────────────────────────────
    try:
        from ..providers.qobuz import _STREAM_APIS, _POST_APIS

        qobuz_eps: list[tuple[str, str]] = []
        _QOBUZ_PROBE_ID = "3135556"
        for url in _STREAM_APIS:
            if url.endswith("="):
                qobuz_eps.append(("GET", f"{url}{_QOBUZ_PROBE_ID}&quality=6"))
            else:
                qobuz_eps.append(("GET", f"{url}{_QOBUZ_PROBE_ID}?quality=6"))
        for url in _POST_APIS:
            qobuz_eps.append(("POST", url))
        qobuz_eps.append(("GET", "https://api.zarz.moe/v1/health"))
        qobuz_eps.append(("GET", "https://qbz.squid.wtf/"))
        endpoints["qobuz"] = qobuz_eps
    except ImportError:
        endpoints["qobuz"] = [("GET", "https://api.zarz.moe/v1/health")]
        endpoints["qobuz"] = [("GET", "https://qbz.squid.wtf/")]

    # ── Deezer ─────────────────────────────────────────────────────────────
    try:
        from ..providers.deezer import _RESOLVER_URL

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
        from ..providers.amazon import API_ENDPOINTS

        amazon_list: list[tuple[str, str]] = []
        for val in API_ENDPOINTS.values():
            if isinstance(val, dict):
                base_url = val.get("base_url", "")
                if base_url:
                    amazon_list.append(("POST", base_url))
            elif isinstance(val, str):
                amazon_list.append(("POST", val))
        amazon_list.append(("GET", "https://api.zarz.moe/v1/health"))
        amazon_list.append(("GET",  "https://amz.squid.wtf/"))
        endpoints["amazon"] = amazon_list
    except ImportError:
        endpoints["amazon"] = [
            ("POST", "https://amz.spotbye.qzz.io/api"),
            ("POST", "https://amazon.spotbye.qzz.io/api"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]

    # ── Apple Music ────────────────────────────────────────────────────────
    try:
        from ..providers.apple_music import API_ENDPOINTS as APPLE_DL_ENDPOINTS

        endpoints["apple"] = [
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]
    except ImportError:
        endpoints["apple"] = [
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]

    # ── SoundCloud ─────────────────────────────────────────────────────────
    try:
        from ..providers.soundcloud import SoundCloudProvider

        sc     = SoundCloudProvider.__new__(SoundCloudProvider)
        cobalt = getattr(sc, "cobalt_api", "https://api.zarz.moe/v1/dl/cobalt/")
        endpoints["soundcloud"] = [
            ("POST", cobalt),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]
    except Exception:
        endpoints["soundcloud"] = [
            ("POST", "https://api.zarz.moe/v1/dl/cobalt/"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]

    # ── YouTube ────────────────────────────────────────────────────────────
    # YouTube usa yt-dlp locale, nessun endpoint HTTP da sondare.
    endpoints["youtube"] = []

    # ── Pandora ────────────────────────────────────────────────────────────
    try:
        from ..providers.pandora import _API_BASE_URL, _DOWNLOAD_PATH

        endpoints["pandora"] = [
            ("GET",  f"{_API_BASE_URL}/v1/health"),
            ("POST", f"{_API_BASE_URL}{_DOWNLOAD_PATH}"),
        ]
    except ImportError:
        endpoints["pandora"] = [("GET", "https://api.zarz.moe/v1/health")]

    # ── GD Studio API (Netease, Kuwo, Migu, Joox) ──────────────────────────
    for provider in ["netease", "kuwo", "migu", "joox"]:
        endpoints[provider] = [
            ("GET", "https://music-api.gdstudio.xyz/api.php"),
            ("GET", "https://music.wjhe.top/api/music/joox/url?ID=11259&quality=1000&format=flac"),
        ]

    # ── FlacDownloader ────────────────────────────────────────────────────
    try:
        from ..providers.flacdownloader import _API_BASE as FLAC_BASE

        endpoints["flacdownloader"] = [
            ("GET", f"{FLAC_BASE}/download-token?t=9997018&f=FLAC")
        ]
    except ImportError:
        endpoints["flacdownloader"] = [
            ("GET", "https://flacdownloader.com/flac/download-token?t=9997018&f=FLAC")
        ]

    return endpoints


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UA                = "SpotiFLAC-HealthCheck/4.5.0"
_TIMEOUT           = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
_MAX_CONCURRENT    = 25
_ZARZ_HEALTH_URL   = "https://api.zarz.moe/v1/health"
_GLOBAL_HC_TIMEOUT = 10  # secondi

# Carica gli endpoint una sola volta al momento dell'import
_ENDPOINTS: dict[str, list[tuple[str, str]]] = _load_endpoints()


def _make_async_client() -> httpx.AsyncClient:
    """
    Crea un AsyncClient con i limiti di connessione appropriati.
    Usato come context manager in run_health_check per garantire cleanup corretto.
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
# Data model  (invariato)
# ---------------------------------------------------------------------------

class HealthResult(NamedTuple):
    provider: str
    url:      str
    method:   str
    ok:       bool
    latency:  float
    detail:   str


# ---------------------------------------------------------------------------
# Core async check logic
# ---------------------------------------------------------------------------

async def _check_one(
    client: httpx.AsyncClient,
    provider: str,
    method: str,
    url: str,
) -> HealthResult:
    """Sonda un singolo endpoint e restituisce un HealthResult."""
    try:
        t0 = time.perf_counter()

        req_kwargs: dict = {"headers": {"User-Agent": _UA}}

        if provider == "flacdownloader":
            req_kwargs["headers"].update({
                "Accept": "*/*",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
                "X-Download-Access": "l@p*gute)77=g5clebcp4lz#=x%(*rwg+ku0_)bh=&%6wg!a",
            })

        if method == "POST" and provider == "deezer":
            req_kwargs["json"] = {
                "platform": "deezer",
                "url": "https://www.deezer.com/track/3135556",
            }

        resp = await client.request(method, url, follow_redirects=True, **req_kwargs)
        ms   = (time.perf_counter() - t0) * 1000

        ok     = False
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
                            ok     = False
                            detail = str(
                                data.get("message") or data.get("error") or "API Error"
                            )[:10]
                        else:
                            ok, detail = True, "HTTP 200 OK"
                    except ValueError:
                        ok, detail = True, "HTTP 200 OK"

            elif resp.status_code >= 500:
                ok = False   # detail già impostato sopra ("HTTP 5xx")

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
                # Qualsiasi altro status (4xx diverso da 401, 3xx già seguiti) →
                # il server è raggiungibile
                ok = True

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
                        if (
                            svc_info.get("status") == 401
                            and svc_info.get("detail") == "auth_required"
                        ):
                            ok, detail = False, "Auth required"
                        elif svc_info.get("ok") is True or svc_info.get("status") == 200:
                            ok, detail = True, svc_info.get("detail") or "ok"
                        else:
                            ok     = False
                            detail = (
                                f"Zarz {svc_info.get('status')} "
                                f"({svc_info.get('detail') or 'error'})"
                            )
                    else:
                        ok, detail = True, "Zarz Link OK"
                except ValueError:
                    detail = "Bad Health Payload"

                return HealthResult(provider, url, method, ok, ms, detail)

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
                            ok, detail = True, parsed.get("error", "JSON OK")
                    except ValueError:
                        detail = "No Stream URL"

            # ── Deezer ─────────────────────────────────────────────────────
            elif provider == "deezer":
                try:
                    parsed = json.loads(body)
                    if parsed.get("id") and not parsed.get("error"):
                        ok, detail = True, "API OK"
                    else:
                        detail = parsed.get("error", {}).get("message", "API Error")
                except ValueError:
                    detail = "Bad JSON"

            # ── FlacDownloader ─────────────────────────────────────────────
            elif provider == "flacdownloader":
                try:
                    parsed = json.loads(body)
                    if "token" in parsed or "expires" in parsed or "error" in parsed:
                        ok, detail = True, "API OK"
                    else:
                        detail = "Unknown JSON"
                except ValueError:
                    detail = "CF Blocked / HTML"

            # ── Apple / SoundCloud / YouTube / default ─────────────────────
            else:
                if body.strip():
                    ok = True
                else:
                    detail = "Empty Body"

        elif resp.status_code in (404, 400):
            # Mirror tidal/qobuz che risponde 404/400 su una track sonda è comunque attivo
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
    """Rispetta il semaforo globale prima di aprire una connessione."""
    async with sem:
        return await _check_one(client, provider, method, url)


async def _zarz_bulk_check(
    client: httpx.AsyncClient,
    services: list[str],
) -> dict[str, HealthResult]:
    """
    Una sola richiesta a Zarz per ricavare lo stato di tutti i provider.
    Ritorna {provider: HealthResult} solo per i provider presenti nella risposta.
    """
    try:
        t0   = time.perf_counter()
        resp = await client.get(_ZARZ_HEALTH_URL, headers={"User-Agent": _UA})
        ms   = (time.perf_counter() - t0) * 1000

        if resp.status_code != 200:
            return {}

        svc_map = json.loads(resp.text).get("services", {})
        out: dict[str, HealthResult] = {}

        for svc in services:
            key = "qobuz" if svc == "qbz" else svc
            if key not in svc_map:
                continue
            info = svc_map[key]
            if info.get("status") == 401 and info.get("detail") == "auth_required":
                ok, detail = False, "Auth required"
            elif info.get("ok") is True or info.get("status") == 200:
                ok, detail = True, info.get("detail") or "ok"
            else:
                ok     = False
                detail = f"Zarz {info.get('status')} ({info.get('detail', 'error')})"

            out[svc] = HealthResult(svc, _ZARZ_HEALTH_URL, "GET", ok, ms, detail)

        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_health_check(
    services: list[str],
    *,
    include_all_endpoints: bool = True,
) -> list[HealthResult]:
    """
    Controlla in modo asincrono la raggiungibilità di tutti i provider indicati.

    Crea un httpx.AsyncClient dedicato per ogni chiamata (context manager) per
    evitare problemi di binding all'event loop in scenari multi-run.
    """
    results:   list[HealthResult]         = []
    task_list: list[tuple[str, str, str]] = []

    # YouTube → sempre locale, nessuna rete
    for svc in services:
        if svc == "youtube":
            results.append(
                HealthResult("youtube", "yt-dlp (local binary)", "CLI", True, 0.0, "local")
            )

    remaining = [s for s in services if s != "youtube"]
    if not remaining:
        return results

    # Un semaforo per limitare le connessioni concorrenti
    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async with _make_async_client() as client:
        # ── Fast-path: una richiesta Zarz per tutti i provider ──────────────
        zarz_results = await _zarz_bulk_check(client, remaining)

        for svc in remaining:
            zarz_r = zarz_results.get(svc)

            if zarz_r and zarz_r.ok:
                # Provider confermato OK da Zarz → nessuna sonda individuale
                results.append(zarz_r)
                continue

            # Provider assente da Zarz o segnalato come down → sonda individuale
            eps = _ENDPOINTS.get(svc)
            if not eps:
                if zarz_r:
                    results.append(zarz_r)   # restituiamo almeno il risultato Zarz
                continue

            # Escludi l'endpoint Zarz (già controllato sopra)
            eps_to_probe = [(m, u) for m, u in eps if "zarz.moe/v1/health" not in u]

            if include_all_endpoints:
                task_list.extend((svc, m, u) for m, u in eps_to_probe)
            elif eps_to_probe:
                m, u = eps_to_probe[0]
                task_list.append((svc, m, u))

        # ── Sonde individuali (solo per provider non già risolti da Zarz) ───
        if task_list:
            # Mappa task → (provider, method, url) per recuperare i metadati
            # sui task ancora pendenti al timeout globale
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

            # Drena i task cancellati per evitare RuntimeWarning
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    # ── Post-processing auth_required ──────────────────────────────────────
    auth_blocked = {r.provider for r in results if "auth" in r.detail.lower() and not r.ok}
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


def run_health_check_sync(
    services: list[str],
    **kwargs,
) -> list[HealthResult]:
    """
    Wrapper sincrono — crea un event loop dedicato tramite asyncio.run().

    Non usare se sei già in un contesto async (es. all'interno di un'altra
    coroutine): chiama direttamente `await run_health_check(...)` invece.
    """
    return asyncio.run(run_health_check(services, **kwargs))


# ---------------------------------------------------------------------------
# Report rendering  (invariato)
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
        symbol  = "✅" if r.ok else "❌"
        lat_str = f"{r.latency:>5.0f} ms" if r.latency >= 0 else "  timeout"
        detail  = r.detail[:10]

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

    ok_count   = sum(1 for r in results if r.ok)
    prov_ok    = len({r.provider for r in results if r.ok})
    prov_total = len({r.provider for r in results})
    print(
        f"\n  {ok_count}/{len(results)} endpoints reachable "
        f"({prov_ok}/{prov_total} providers with at least one working endpoint).\n"
    )


# ---------------------------------------------------------------------------
# Convenience helpers  (invariato)
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
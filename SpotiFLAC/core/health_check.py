from __future__ import annotations

import asyncio
import json
import time
from typing import NamedTuple
from urllib.parse import urlparse

import httpx


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
# Import endpoint lists directly from provider modules
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
        endpoints["tidal"] = [
            ("POST", "https://api.zarz.moe/v1/dl/tid2"),
            ("GET", "https://api.zarz.moe/v1/health")
        ]

    # ── Qobuz ──────────────────────────────────────────────────────────────
    try:
        from ..providers.qobuz import (
            _STREAM_APIS, _POST_APIS, _FLACDOWNLOADER_APIS, 
            _QOBUZ_DL_, _GDSTUDIO_APIS, _WJHE_APIS
        )

        qobuz_eps: list[tuple[str, str]] = []
        _QOBUZ_PROBE_ID = "3135556"
        
        for url in _STREAM_APIS:
            if url.endswith("="):
                qobuz_eps.append(("GET", f"{url}{_QOBUZ_PROBE_ID}&quality=6"))
            else:
                qobuz_eps.append(("GET", f"{url}{_QOBUZ_PROBE_ID}?quality=6"))
        
        for url in _QOBUZ_DL_:
            qobuz_eps.append(("GET", f"{url}track_id={_QOBUZ_PROBE_ID}&quality=6"))
            
        for url in _POST_APIS:
            qobuz_eps.append(("POST", url))
            
        for url in _GDSTUDIO_APIS:
            qobuz_eps.append(("POST", url))
            
        for url in _WJHE_APIS:
            qobuz_eps.append(("GET", f"{url}?ID={_QOBUZ_PROBE_ID}&quality=1000&format=flac"))
            
        for url in _FLACDOWNLOADER_APIS:
            qobuz_eps.append(("GET", f"{url.rstrip('/')}/prepare"))
            
        qobuz_eps.append(("GET", "https://api.zarz.moe/v1/health"))
        qobuz_eps.append(("GET", "https://qbz.squid.wtf/"))
        endpoints["qobuz"] = qobuz_eps
    except ImportError:
        endpoints["qobuz"] = [
            ("GET", "https://qbz.afkarxyz.qzz.io/api/track/3135556?quality=6"),
            ("GET", "https://qobuz.spotbye.qzz.io/api/track/3135556?quality=6"),
            ("GET", "https://qobuz.kennyy.com.br/api/download-music?track_id=3135556&quality=6"),
            ("GET", "https://qobuz.squid.wtf/api/download-music?track_id=3135556&quality=6"),
            ("GET", "https://mono.scavengerfurs.net/api/download-music?track_id=3135556&quality=6"),
            ("POST", "https://api.zarz.moe/v1/dl/qbz"),
            ("POST", "https://api.zarz.moe/v1/dl/qbz2"),
            ("POST", "https://music.gdstudio.xyz/api.php"),
            ("POST", "https://music.gdstudio.org/api.php"),
            ("GET", "https://music.wjhe.top/api/music/qobuz/url?ID=3135556&quality=1000&format=flac"),
            ("GET", "https://flacdownloader.com/prepare"),
            ("GET", "https://api.zarz.moe/v1/health"),
            ("GET", "https://qbz.squid.wtf/")
        ]

    # ── Deezer ─────────────────────────────────────────────────────────────
    try:
        from ..providers.deezer import _RESOLVER_URL
        endpoints["deezer"] = [
            ("POST", _RESOLVER_URL),
            ("GET", "https://flacdownloader.com/prepare"),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]
    except ImportError:
        endpoints["deezer"] = [
            ("POST", "https://api.zarz.moe/v1/dl/dzr"),
            ("GET", "https://flacdownloader.com/prepare"),
            ("GET", "https://api.zarz.moe/v1/health"),
        ]

    # ── Amazon ─────────────────────────────────────────────────────────────
    try:
        from ..providers.amazon import API_ENDPOINTS, _SQUID_BASE
        amazon_list: list[tuple[str, str]] = []
        for val in API_ENDPOINTS.values():
            base_url = val.get("base_url")
            method = val.get("method", "GET").upper()
            if base_url:
                amazon_list.append((method, base_url))
        
        amazon_list.append(("GET", f"{_SQUID_BASE}/captcha/challenge"))
        amazon_list.append(("POST", "https://dl.musicdl.me/download"))
        amazon_list.append(("GET", "https://api.zarz.moe/v1/health"))
        amazon_list.append(("GET", "https://amz.squid.wtf/"))
        endpoints["amazon"] = amazon_list
    except ImportError:
        endpoints["amazon"] = [
            ("POST", "https://amz.spotbye.qzz.io/api"),
            ("GET", "https://amazon.spotbye.qzz.io/api"),
            ("GET", "https://api.zarz.moe/v1/dl/amazeamazeamaze"),
            ("GET", "https://amz.squid.wtf/api/captcha/challenge"),
            ("POST", "https://dl.musicdl.me/download"),
            ("GET", "https://api.zarz.moe/v1/health"),
            ("GET", "https://amz.squid.wtf/")
        ]

    # ── Apple Music ────────────────────────────────────────────────────────
    try:
        from ..providers.apple_music import API_ENDPOINTS as APPLE_DL_ENDPOINTS
        endpoints["apple"] = [
            ("POST", APPLE_DL_ENDPOINTS.get("proxy_direct", "https://api.zarz.moe/v1/dl/app2")),
            ("POST", f"{APPLE_DL_ENDPOINTS.get('proxy_queued', 'https://api.zarz.moe/v1/dl/app')}/download"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]
    except ImportError:
        endpoints["apple"] = [
            ("POST", "https://api.zarz.moe/v1/dl/app2"),
            ("POST", "https://api.zarz.moe/v1/dl/app/download"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]

    # ── SoundCloud ─────────────────────────────────────────────────────────
    try:
        from ..providers.soundcloud import SoundCloudProvider
        sc     = SoundCloudProvider.__new__(SoundCloudProvider)
        cobalt = getattr(sc, "cobalt_api", "https://api.zarz.moe/v1/dl/cobalt/")
        endpoints["soundcloud"] = [
            ("POST", cobalt),
            ("POST", "https://co.wuk.sh/"),
            ("POST", "https://cobalt.qiaeru.tech/"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]
    except Exception:
        endpoints["soundcloud"] = [
            ("POST", "https://api.zarz.moe/v1/dl/cobalt/"),
            ("POST", "https://co.wuk.sh/"),
            ("POST", "https://cobalt.qiaeru.tech/"),
            ("GET",  "https://api.zarz.moe/v1/health"),
        ]

    # ── YouTube ────────────────────────────────────────────────────────────
    endpoints["youtube"] = [
        ("POST", "https://api.zarz.moe/v1/dl"),
        ("POST", "https://co.wuk.sh/"),
        ("POST", "https://cobalt.qiaeru.tech/"),
        ("POST", "https://yt1d.io/wp-admin/admin-ajax.php")
    ]

    # ── Pandora ────────────────────────────────────────────────────────────
    try:
        from ..providers.pandora import _API_BASE_URL, _DOWNLOAD_PATH
        endpoints["pandora"] = [
            ("POST", f"{_API_BASE_URL}{_DOWNLOAD_PATH}"),
            ("GET",  f"{_API_BASE_URL}/v1/health"),
        ]
    except ImportError:
        endpoints["pandora"] = [
            ("POST", "https://api.zarz.moe/v1/dl/pan"),
            ("GET", "https://api.zarz.moe/v1/health")
        ]

    # ── GD Studio API & WJHE (Netease, Kuwo, Migu, Joox) ───────────────────
    endpoints["netease"] = [
        ("GET", "https://music-api.gdstudio.xyz/api.php")
    ]
    endpoints["kuwo"] = [
        ("GET", "https://music-api.gdstudio.xyz/api.php")
    ]
    endpoints["migu"] = [
        ("GET", "https://music.wjhe.top/api.php"),
        ("GET", "https://music.wjhe.top/api/music/migu/url")
    ]
    endpoints["joox"] = [
        ("GET", "https://music-api.gdstudio.xyz/api.php"),
        ("GET", "https://music.wjhe.top/api/music/joox/url?ID=11259&quality=1000&format=flac")
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
                # il server è raggiungibile, anche in assenza di body coretto (400 Bad Request)
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
                    # Supporta sia l'API Deezer ("id") sia flacdownloader ("t")
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
            # Mirror tidal/qobuz/wuk/ecc che risponde 404/400 su una track sonda è comunque attivo
            if provider in ("tidal", "qobuz", "qbz", "youtube", "soundcloud"):
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

    # YouTube: includiamo il check del binario locale, ma manteniamo 
    # il provider in lista in modo che vengano testati anche Cobalt e YT1D.
    for svc in services:
        if svc == "youtube":
            results.append(
                HealthResult("youtube", "yt-dlp (local binary)", "CLI", True, 0.0, "local")
            )

    remaining = list(services)
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
"""
Gestione della sessione "Monochrome" (amz.geeked.wtf, usato da monochrome.tf
per il download Amazon Music).

Protocollo confermato via HAR (entry POST amz.geeked.wtf/api/auth/turnstile):

    POST https://amz.geeked.wtf/api/auth/turnstile
    Content-Type: application/json
    Body: {"cf_turnstile_response": "<token del widget Cloudflare Turnstile>"}

    -> 200 OK
    {
      "access_token": "<JWT>",
      "token_type": "JWT",
      "expires_in": 3600
    }

L'`access_token` è il valore da inviare come header `X-Turnstile-JWT` in ogni
richiesta a `mono` (amz.geeked.wtf/api/track/). Nessuna firma HMAC
per-richiesta: è un bearer semplice.

A differenza del flusso "community" (signed_session_desktop.py), qui NON
esiste un meccanismo di redirect/callback verso un server locale: monochrome
.tf è un sito di terze parti che non sappiamo far puntare a `localhost`. Il
sito carica un widget Cloudflare Turnstile in modalità "invisible" (sitekey
osservato: 0x4AAAAAADgxqF6QVMm0GLHH) e, una volta risolto (di norma senza
alcuna interazione visibile dell'utente), il suo stesso JS effettua la POST
sopra in automatico.

Per questo l'unico modo per ottenere l'access_token è caricare la vera
pagina https://monochrome.tf/ in un browser reale e intercettare la risposta
di quella chiamata di rete — non reinventiamo la POST lato Python perché il
token cf_turnstile_response richiede di superare per davvero la verifica
Cloudflare (JS/WASM), cosa che non si può simulare con semplici richieste
HTTP.

=== Aggiornamento: rimosso Playwright ===
Prima questo modulo apriva un proprio browser Playwright isolato
(`playwright.sync_api.sync_playwright`), duplicando la logica di avvio
browser/profilo/Xvfb già presente in `core/solver.py` (usato da
signed_session_desktop.py e signed_session_mobile.py per Turnstile).
Ora riusa direttamente quel motore (nodriver + CDP): stesso rilevamento
Chrome/Chromium/Edge/Brave (`_find_chrome`), stesso profilo persistente
(`_get_profile_dir`), stessi flag Docker (`_docker_flags`) e stesso avvio
di Xvfb su Linux headless (`_ensure_xvfb`). L'unica differenza rispetto al
solver "grant" standard è cosa cerchiamo nel traffico di rete intercettato:
qui non c'è un grant da cliccare/estrarre da un redirect, ma la risposta
JSON della POST che la pagina fa da sé verso l'endpoint Turnstile, da cui
leggiamo il campo "access_token".

Nota: Cloudflare può rilevare browser headless e presentare comunque una
sfida interattiva o rifiutare il token in alcuni casi; per questo resta un
fallback manuale via terminale.
"""

import os
import json
import time
import asyncio
import base64
import threading
import warnings
import dataclasses
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import logging

from nodriver import cdp
import nodriver as uc

from ..core.endpoints import get_amazon_endpoint
from ..core.solver import (
    _docker_flags,
    _ensure_xvfb,
    _find_chrome,
    _get_profile_dir,
    _patch_nodriver_unknown_cdp_events,
)
from urllib.parse import urlencode
import atexit

logger = logging.getLogger(__name__)

# Costanti
MONOCHROME_SESSION_SKEW = timedelta(minutes=2)
MONOCHROME_VERIFY_TIMEOUT = 60.0
MONOCHROME_PAGE_URL = "https://monochrome.tf/"

_patch_nodriver_unknown_cdp_events()


def _monochrome_turnstile_endpoint_hint() -> str:
    """
    Frammento di URL usato per riconoscere, nel traffico di rete intercettato,
    la risposta della chiamata che monochrome.tf fa da sé per scambiare il
    turnstile token con l'access_token. Letto dal registry (amazon.mono_verify)
    così resta aggiornabile senza toccare il codice.
    """
    url = get_amazon_endpoint("mono_verify")
    return url


_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

monochrome_session_mu = threading.Lock()


@dataclass
class MonochromeSessionRecord:
    jwt: str = ""
    expires_at: str = ""
    user_agent: str = ""
    sec_ch_ua: str = ""
    sec_ch_ua_mobile: str = ""
    sec_ch_ua_platform: str = ""


def ensure_app_dir() -> str:
    app_dir = os.path.expanduser("~/.spotiflac")
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


def monochrome_session_path() -> str:
    directory = ensure_app_dir()
    os.chmod(directory, 0o700)

    signed_sessions_dir = os.path.join(directory, "signed_sessions")
    os.makedirs(signed_sessions_dir, exist_ok=True)
    os.chmod(signed_sessions_dir, 0o700)

    return os.path.join(signed_sessions_dir, "monochrome_sessions.json")


def load_monochrome_session() -> MonochromeSessionRecord:
    path = monochrome_session_path()
    record = MonochromeSessionRecord()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Filtra le chiavi extra per compatibilità all'indietro se ci sono sessioni vecchie
                valid_keys = {f.name for f in dataclasses.fields(MonochromeSessionRecord)}
                filtered_data = {k: v for k, v in data.items() if k in valid_keys}
                record = MonochromeSessionRecord(**filtered_data)
        except Exception:
            pass

    return record


def save_monochrome_session(record: MonochromeSessionRecord):
    path = monochrome_session_path()
    data = json.dumps(asdict(record), indent=2)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(data)

    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)
    os.chmod(path, 0o600)


def _decode_jwt_exp(token: str) -> datetime | None:
    """
    Estrae il claim "exp" dal payload del JWT senza verificarne la firma
    (non abbiamo il secret: il client si fida del token perché lo rimanda
    allo stesso server che lo ha emesso, esattamente come farebbe il sito).
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        exp = payload.get("exp")
        if not exp:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None


def monochrome_session_valid(record: MonochromeSessionRecord) -> bool:
    if not record or not record.jwt or not record.expires_at:
        return False
    try:
        expires_str = record.expires_at.replace("Z", "+00:00")
        expires_at = datetime.fromisoformat(expires_str)
        return (expires_at - datetime.now(timezone.utc)) > MONOCHROME_SESSION_SKEW
    except Exception:
        return False


def ensure_monochrome_session() -> MonochromeSessionRecord:
    with monochrome_session_mu:
        record = load_monochrome_session()

        if monochrome_session_valid(record):
            return record

        auth_data = run_monochrome_verification()
        access_token = auth_data["access_token"]

        exp_dt = _decode_jwt_exp(access_token)
        if exp_dt is None:
            # Fallback prudenziale se per qualche motivo non si riesce a
            # decodificare l'exp (es. token incollato manualmente male).
            exp_dt = datetime.now(timezone.utc) + timedelta(minutes=55)
            logger.warning(
                "Impossibile decodificare l'exp dal JWT Monochrome: uso scadenza locale prudenziale."
            )

        record.jwt = access_token
        record.expires_at = exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        record.user_agent = auth_data.get("user_agent", _DESKTOP_UA)
        record.sec_ch_ua = auth_data.get("sec_ch_ua", "")
        record.sec_ch_ua_mobile = auth_data.get("sec_ch_ua_mobile", "?0")
        record.sec_ch_ua_platform = auth_data.get("sec_ch_ua_platform", '"macOS"')

        save_monochrome_session(record)
        return record


def clear_monochrome_session_credentials():
    with monochrome_session_mu:
        try:
            record = load_monochrome_session()
            record.jwt = ""
            record.expires_at = ""
            save_monochrome_session(record)
        except Exception:
            pass


def _run_manual_terminal_verification() -> dict:
    """
    Fallback da terminale (o Docker/Telegram/risoluzione automatica fallita).
    Restituisce anche i Client Hints simulati di default.
    """
    print(
        f"\n[SpotiFLAC] Verifica Monochrome richiesta.\n"
        f"  1. Apri {MONOCHROME_PAGE_URL} in un browser normale.\n"
        f"  2. In DevTools -> Network, cerca la richiesta POST verso\n"
        f"     '{_monochrome_turnstile_endpoint_hint()}'.\n"
        f"  3. Copia il campo 'access_token' dalla risposta JSON\n"
        f"     (oppure l'header 'X-Turnstile-JWT' da una richiesta successiva\n"
        f"     verso amz.geeked.wtf/api/track/).\n"
    )
    try:
        token = input("Incolla qui l'access_token: ").strip()
        if not token:
            raise RuntimeError("No access_token provided.")
        return {
            "access_token": token,
            "user_agent": _DESKTOP_UA,
            "sec_ch_ua": '"Not(A:Brand";v="99", "Google Chrome";v="150", "Chromium";v="150"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"macOS"'
        }
    except EOFError:
        raise Exception("verification cancelled (EOF)")


async def _solve_via_monochrome_page_async(timeout: float) -> dict:
    import random
    
    result: dict = {}

    browser = await uc.start(
        browser_executable_path=_find_chrome(),
        headless=False,
        user_data_dir=_get_profile_dir(),
        browser_args=[
            "--incognito",
            # Flag per prevenire il throttle della CPU/JS in background,
            # in modo che Turnstile non si blocchi se la finestra è minimizzata
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--window-size=1280,900",
            *_docker_flags,
        ],
    )

    page = None

    async def _on_response(event) -> None:
        if "access_token" in result:
            return
        try:
            resp = event.response
            if "auth/turnstile" not in resp.url:
                return
            
            mime = (getattr(resp, "mime_type", "") or "").lower()
            if "json" not in mime:
                return

            body, is_base64 = await page.send(
                cdp.network.get_response_body(event.request_id)
            )
            if is_base64:
                try:
                    body = base64.b64decode(body).decode("utf-8", errors="ignore")
                except Exception:
                    return

            data = json.loads(body)
            if not isinstance(data, dict):
                return

            token = data.get("access_token")
            
            # Verifichiamo che sia un JWT reale (è sempre una stringa lunga e divisa da punti)
            if isinstance(token, str) and token.strip() and len(token) > 30:
                result["access_token"] = token.strip()
                
                # Catturiamo gli header reali di rete inviati dal browser
                req_headers = getattr(resp, "request_headers", {})
                if not req_headers and hasattr(resp, "requestHeaders"):
                    req_headers = resp.requestHeaders

                def get_h(k):
                    return next((v for key, v in req_headers.items() if key.lower() == k.lower()), "")

                result["user_agent"] = get_h("user-agent")
                result["sec_ch_ua"] = get_h("sec-ch-ua")
                result["sec_ch_ua_mobile"] = get_h("sec-ch-ua-mobile")
                result["sec_ch_ua_platform"] = get_h("sec-ch-ua-platform")

        except Exception:
            pass

    try:
        page = await browser.get(MONOCHROME_PAGE_URL)

        try:
            if hasattr(page, "minimize"):
                await page.minimize()
        except Exception:
            pass

        try:
            await page.send(cdp.network.enable())
            page.add_handler(cdp.network.ResponseReceived, _on_response)
        except Exception as exc:
            logger.debug("[monochrome] network capture unavailable: %s", exc)

        async def get_cf_iframe_rect():
            raw = await page.evaluate('''
                JSON.stringify((() => {
                    for (const f of document.querySelectorAll('iframe')) {
                        const src = f.src || f.getAttribute('src') || '';
                        if (!src.includes('challenges.cloudflare.com')) continue;
                        const r = f.getBoundingClientRect();
                        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
                    }
                    return null;
                })())
            ''')
            if raw and raw != "null":
                return json.loads(raw)
            return None

        deadline = time.monotonic() + timeout
        click_count = 0
        
        while "access_token" not in result and time.monotonic() < deadline:
            
            chunk_deadline = min(time.monotonic() + 10.0, deadline)
            
            while "access_token" not in result and time.monotonic() < chunk_deadline:
                rect = await get_cf_iframe_rect()
                if rect and click_count < 5:
                    cx = rect["x"] + 28 + random.uniform(-3, 3)
                    cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                    await page.mouse_move(cx - 80, cy - 20)
                    await asyncio.sleep(random.uniform(0.15, 0.25))
                    await page.mouse_move(cx, cy)
                    await asyncio.sleep(random.uniform(0.08, 0.15))
                    await page.mouse_click(cx, cy)
                    click_count += 1
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.5)

            if "access_token" not in result and time.monotonic() < deadline:
                logger.info("[monochrome] Nessun token ricevuto in 10 secondi. Ricarico la pagina...")
                try:
                    await page.reload()
                    await asyncio.sleep(2.0)
                except Exception:
                    pass

    finally:
        browser.stop()

    if "access_token" not in result:
        raise Exception(f"Timeout: nessun access_token JWT catturato entro {timeout:.0f}s")
        
    # Fallback JS se il CDP non ha popolato i request_headers
    if (not result.get("user_agent") or not result.get("sec_ch_ua")) and page:
        try:
            ua_info = await page.evaluate('''
                () => {
                    let brands = "";
                    let mobile = "?0";
                    let platform = "";
                    if (navigator.userAgentData) {
                        brands = navigator.userAgentData.brands.map(b => `"${b.brand}";v="${b.version}"`).join(", ");
                        mobile = navigator.userAgentData.mobile ? "?1" : "?0";
                        platform = `"${navigator.userAgentData.platform}"`;
                    }
                    return {
                        ua: navigator.userAgent,
                        brands: brands,
                        mobile: mobile,
                        platform: platform
                    };
                }
            ''')
            result["user_agent"] = ua_info.get("ua", _DESKTOP_UA)
            result["sec_ch_ua"] = ua_info.get("brands", "")
            result["sec_ch_ua_mobile"] = ua_info.get("mobile", "?0")
            result["sec_ch_ua_platform"] = ua_info.get("platform", "")
        except Exception as e:
            logger.debug(f"[monochrome] Fallback UA JS failed: {e}")

    return result

def _solve_via_monochrome_page(timeout: float) -> dict:
    """Wrapper sincrono di _solve_via_monochrome_page_async."""
    _ensure_xvfb()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return asyncio.run(_solve_via_monochrome_page_async(timeout))


def run_monochrome_verification() -> dict:
    """
    Esegue la verifica Turnstile per Monochrome e ritorna i dati necessari:
    (JWT access_token e il Client Hint/User-Agent).
    """
    try:
        auth_data = _solve_via_monochrome_page(MONOCHROME_VERIFY_TIMEOUT)
        logger.info("Automated Monochrome verification successful!")
        return auth_data
    except Exception as e:
        logger.warning(f"Automated Monochrome verification failed: {e}")

    logger.info("Falling back to manual terminal input.")
    return _run_manual_terminal_verification()


def get_monochrome_auth_headers() -> dict:
    record = ensure_monochrome_session()

    headers = {
        "X-Turnstile-JWT": record.jwt,
    }
    if record.user_agent:
        headers["User-Agent"] = record.user_agent
    if record.sec_ch_ua:
        headers["sec-ch-ua"] = record.sec_ch_ua
    else:
        headers["sec-ch-ua"] = '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"'
    if record.sec_ch_ua_mobile:
        headers["sec-ch-ua-mobile"] = record.sec_ch_ua_mobile
    if record.sec_ch_ua_platform:
        headers["sec-ch-ua-platform"] = record.sec_ch_ua_platform

    return headers


class _MonochromeBrowserSession:
    """
    Mantiene un browser CDP persistente per instradare le richieste
    all'API mono (amz.geeked.wtf) DENTRO la sessione TLS/fingerprint reale
    del browser, non tramite httpx. Necessario perché il backend valida
    il claim 'fp' del JWT contro segnali di rete (verosimilmente TLS
    JA3/JA4) che un client HTTP puro non può replicare — un token
    ottenuto via CDP ma riusato da httpx viene sistematicamente rifiutato
    con 401, anche con header identici byte-per-byte a quelli del browser
    (verificato via confronto con HAR).
    """

    def __init__(self) -> None:
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()
        self._record = MonochromeSessionRecord()
        self._ever_solved = False

    async def _ensure_browser(self) -> None:
        if self._browser is not None and self._page is not None:
            return
        _ensure_xvfb()
        self._browser = await uc.start(
            browser_executable_path=_find_chrome(),
            headless=False,
            user_data_dir=_get_profile_dir(),
            browser_args=[
                "--incognito",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--window-size=1280,900",
                *_docker_flags,
            ],
        )
        self._page = await self._browser.get(MONOCHROME_PAGE_URL)

        try:
            if hasattr(self._page, "minimize"):
                await self._page.minimize()
        except Exception:
            pass

    async def _solve_turnstile_on_page(self, timeout: float) -> str:
        result: dict = {}

        async def _on_response(event) -> None:
            if "access_token" in result:
                return
            try:
                resp = event.response
                if "auth/turnstile" not in resp.url:
                    return
                mime = (getattr(resp, "mime_type", "") or "").lower()
                if "json" not in mime:
                    return
                body, is_base64 = await self._page.send(
                    cdp.network.get_response_body(event.request_id)
                )
                if is_base64:
                    try:
                        body = base64.b64decode(body).decode("utf-8", errors="ignore")
                    except Exception:
                        return
                data = json.loads(body)
                if not isinstance(data, dict):
                    return
                token = data.get("access_token")
                if isinstance(token, str) and token.strip() and len(token) > 30:
                    result["access_token"] = token.strip()
            except Exception:
                pass

        try:
            await self._page.send(cdp.network.enable())
            self._page.add_handler(cdp.network.ResponseReceived, _on_response)
        except Exception as exc:
            logger.debug("[monochrome] network capture unavailable: %s", exc)

        if self._ever_solved:
            try:
                await self._page.reload()
                await asyncio.sleep(1.0)
            except Exception:
                pass

        async def get_cf_iframe_rect():
            raw = await self._page.evaluate('''
                JSON.stringify((() => {
                    for (const f of document.querySelectorAll('iframe')) {
                        const src = f.src || f.getAttribute('src') || '';
                        if (!src.includes('challenges.cloudflare.com')) continue;
                        const r = f.getBoundingClientRect();
                        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
                    }
                    return null;
                })())
            ''')
            if raw and raw != "null":
                return json.loads(raw)
            return None

        import random
        deadline = time.monotonic() + timeout
        click_count = 0
        reload_count = 0  # <--- AGGIUNGIAMO IL CONTATORE
        
        while "access_token" not in result and time.monotonic() < deadline:
            
            # Aspettiamo fino a 10 secondi per questo ciclo
            chunk_deadline = min(time.monotonic() + 10.0, deadline)
            
            while "access_token" not in result and time.monotonic() < chunk_deadline:
                rect = await get_cf_iframe_rect()
                if rect and click_count < 5:
                    cx = rect["x"] + 28 + random.uniform(-3, 3)
                    cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                    await self._page.mouse_move(cx - 80, cy - 20)
                    await asyncio.sleep(random.uniform(0.15, 0.25))
                    await self._page.mouse_move(cx, cy)
                    await asyncio.sleep(random.uniform(0.08, 0.15))
                    await self._page.mouse_click(cx, cy)
                    click_count += 1
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.5)

            if "access_token" not in result and time.monotonic() < deadline:
                if reload_count < 2:
                    reload_count += 1
                    logger.info(f"[mono] No token received. Refreshing the page (attempt {reload_count}/2)...")
                    try:
                        await self._page.reload()
                        await asyncio.sleep(2.0)
                    except Exception:
                        pass
                else:
                    logger.warning("[mono] Max attempt, no token received.")
                    break

        if "access_token" not in result:
            raise Exception(f"Timeout: nessun access_token JWT catturato entro {timeout:.0f}s")

        self._ever_solved = True
        return result["access_token"]
    
    async def _ensure_token(self) -> str:
        if monochrome_session_valid(self._record):
            return self._record.jwt

        await self._ensure_browser()
        token = await self._solve_turnstile_on_page(MONOCHROME_VERIFY_TIMEOUT)

        exp_dt = _decode_jwt_exp(token) or (
            datetime.now(timezone.utc) + timedelta(minutes=55)
        )
        self._record.jwt = token
        self._record.expires_at = exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        save_monochrome_session(self._record)
        return token

    async def _do_fetch(self, full_url: str, token: str) -> dict:
        raw = await self._page.evaluate(
            f'''
            (async () => {{
                try {{
                    const r = await fetch({json.dumps(full_url)}, {{
                        headers: {{ "X-Turnstile-JWT": {json.dumps(token)} }}
                    }});
                    const text = await r.text();
                    return JSON.stringify({{ok: r.ok, status: r.status, body: text}});
                }} catch (e) {{
                    return JSON.stringify({{ok: false, status: 0, body: String(e)}});
                }}
            }})()
            ''',
            await_promise=True,
        )
        return json.loads(raw)

    async def fetch_track(self, params: dict) -> dict:
        async with self._lock:
            return await self._fetch_track_with_restart(params, allow_restart=True)

    async def _fetch_track_with_restart(self, params: dict, *, allow_restart: bool) -> dict:
        await self._ensure_browser()
        token = await self._ensure_token()

        mono_url = get_amazon_endpoint("mono")
        qs = urlencode(params)
        sep = "&" if "?" in mono_url else "?"
        full_url = f"{mono_url.rstrip('/') if '?' not in mono_url else mono_url}{sep}{qs}"

        try:
            outer = await asyncio.wait_for(
                self._do_fetch(full_url, token), timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[monochrome] track request timed out after 10s — restarting browser…"
            )
            await self._hard_reset()
            if not allow_restart:
                raise RuntimeError("mono API request timed out after browser restart")
            return await self._fetch_track_with_restart(params, allow_restart=False)

        if not outer.get("ok") and outer.get("status") == 401:
            # Sessione invalidata lato server: forza un nuovo solve e riprova UNA volta.
            self._record = MonochromeSessionRecord()
            token = await self._ensure_token()
            try:
                outer = await asyncio.wait_for(
                    self._do_fetch(full_url, token), timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[monochrome] retry after 401 also timed out — restarting browser…"
                )
                await self._hard_reset()
                if not allow_restart:
                    raise RuntimeError("mono API request timed out after browser restart")
                return await self._fetch_track_with_restart(params, allow_restart=False)

        if not outer.get("ok"):
            raise RuntimeError(
                f"mono API (in-browser) returned {outer.get('status')}: "
                f"{outer.get('body', '')[:200]}"
            )

        try:
            return json.loads(outer["body"])
        except Exception as exc:
            raise RuntimeError(f"mono API returned invalid JSON: {exc}") from exc

    async def _hard_reset(self) -> None:
        """Chiude il browser e resetta il token: forza un browser nuovo di zecca al prossimo tentativo."""
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception:
                pass
        self._browser = None
        self._page = None
        self._ever_solved = False
        self._record = MonochromeSessionRecord()

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    self._browser.stop()
                except Exception:
                    pass
            self._browser = None
            self._page = None


_mono_browser_session = _MonochromeBrowserSession()


async def fetch_mono_track_via_browser(params: dict) -> dict:
    """Esegue la GET /api/track/ instradata dentro la sessione CDP del browser."""
    return await _mono_browser_session.fetch_track(params)


async def close_mono_browser_session() -> None:
    await _mono_browser_session.close()


def _close_mono_browser_sync() -> None:
    try:
        asyncio.run(close_mono_browser_session())
    except Exception:
        pass


atexit.register(_close_mono_browser_sync)
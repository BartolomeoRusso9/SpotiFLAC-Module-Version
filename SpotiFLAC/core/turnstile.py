import asyncio
import json
import os
import platform
import random
import subprocess
import threading
import time
import base64 as _b64
import logging as _logging
import logging
from nodriver import cdp
from typing import Optional
from urllib.parse import parse_qsl, urlparse
import nodriver as uc
import inspect
import os as _os

logger = logging.getLogger(__name__)

DEFAULT_TURNSTILE_CACHE_TTL_SECONDS = 900
_TURNSTILE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_RELOAD_CHECK_SECONDS = 10.0
_MAX_RELOAD_ATTEMPTS = 3



_docker_flags = []
if _os.name != "nt" and hasattr(_os, "geteuid") and _os.geteuid() == 0:
    _docker_flags = ["--no-sandbox", "--disable-dev-shm-usage"]


def _patch_nodriver_unknown_cdp_events() -> None:
    """
    Le build recenti di Chrome emettono eventi CDP nuovi (es. l'heartbeat
    di ad-tagging 'Network.requestAdblockInfoReceived') che il registro
    tipizzato di nodriver non conosce ancora. Senza questa patch, ricevere
    UN SOLO evento del genere solleva un KeyError non catturato dentro il
    loop interno di gestione messaggi di nodriver, uccidendo l'intera
    connessione CDP (e quindi la sessione del browser) — anche se
    l'evento in sé è del tutto irrilevante per noi.

    Firma flessibile (*args, **kwargs): la versione installata di nodriver
    chiama process_event con argomenti posizionali aggiuntivi oltre al
    messaggio (es. self.process_event(message, None)) — una firma rigida
    (self, message) rompe la chiamata reale con un TypeError.

    Idempotente: sicuro da chiamare più volte (applica la patch una
    sola volta).
    """
    from nodriver.core import connection as _nd_connection

    if getattr(_nd_connection, "_spotiflac_unknown_event_patch", False):
        return

    _original = _nd_connection.Connection.process_event

    if inspect.iscoroutinefunction(_original):

        async def _patched(self, *args, **kwargs):
            try:
                return await _original(self, *args, **kwargs)
            except KeyError as exc:
                import logging

                logging.getLogger(__name__).debug(
                    "[turnstile] ignoring unknown CDP event: %s", exc
                )
                return None

    else:

        def _patched(self, *args, **kwargs):
            try:
                return _original(self, *args, **kwargs)
            except KeyError as exc:
                import logging

                logging.getLogger(__name__).debug(
                    "[turnstile] ignoring unknown CDP event: %s", exc
                )
                return None

    _nd_connection.Connection.process_event = _patched
    _nd_connection._spotiflac_unknown_event_patch = True


_patch_nodriver_unknown_cdp_events()

_logging.getLogger("nodriver.core.connection").setLevel(_logging.CRITICAL)
_logging.getLogger("asyncio").setLevel(_logging.ERROR)


def _find_chrome() -> str:
    """Return the Chrome executable path, checking common locations per OS, including macOS and alternative Chromium browsers."""
    import platform
    import os
    import shutil

    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    if os.environ.get("BRAVE_PATH"):
        return os.environ["BRAVE_PATH"]

    system = platform.system()
    candidates = []

    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",  # Edge
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",  # Brave
        ]
    elif system == "Darwin":  # macOS
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Arc.app/Contents/MacOS/Arc",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser Helper (Renderer).app/Contents/MacOS/Brave Browser Helper (Renderer)",
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/brave-browser",
            "/usr/bin/microsoft-edge-stable",
        ]

    # 1. Controlla i percorsi standard
    for path in candidates:
        if os.path.exists(path):
            return path

    # 2. Ricerca dinamica nelle variabili d'ambiente globali (PATH)
    for cmd in [
        "google-chrome",
        "chrome",
        "chromium",
        "chromium-browser",
        "msedge",
        "brave",
    ]:
        path = shutil.which(cmd)
        if path:
            return path

    raise FileNotFoundError(
        "No Chromium-based browser (Chrome, Edge, Brave, Arc) found on system. "
        "Install one of these browsers or set the CHROME_PATH environment variable."
    )


def _get_profile_dir() -> str:
    """Return a persistent Chrome profile directory for the current OS."""
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    if platform.system() == "Windows":
        base = os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Temp"
        return os.path.join(base, "ts_profile")
    return "/tmp/ts_profile"


def _start_xvfb_if_needed() -> Optional[subprocess.Popen]:
    """On Linux headless servers, start a virtual display so Chrome can run."""
    if platform.system() != "Linux":
        return None
    if os.environ.get("DISPLAY"):
        return None
    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)
    return proc


async def _solve_impl(
    sitekey: str,
    siteurl: str,
    timeout: int,
    capture_callback: bool = False,
    hold_open_seconds: float = 0.0,
) -> str | tuple[str, Optional[str]]:
    browser = await uc.start(
        browser_executable_path=_find_chrome(),
        headless=False,
        user_data_dir=_get_profile_dir(),
        browser_args=[
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
            *_docker_flags,
        ],
    )

    page = None
    callback_grant = _extract_grant_from_callback_url(siteurl)
    network_grant: dict[str, Optional[str]] = {"value": None}

    async def _on_response(event) -> None:
        if not capture_callback or page is None:
            return
        try:
            resp = event.response
            mime = (getattr(resp, "mime_type", "") or "").lower()
            if "json" not in mime:
                return
            body, is_base64 = await page.send(
                cdp.network.get_response_body(event.request_id)
            )
            if is_base64:
                try:
                    body = _b64.b64decode(body).decode("utf-8", errors="ignore")
                except Exception:
                    return
            data = json.loads(body)
            if not isinstance(data, dict):
                return
            grant_val = data.get("grant")
            if isinstance(grant_val, str) and grant_val.strip():
                network_grant["value"] = grant_val.strip()
                logger.debug("[solver:net] grant catturato dalla rete")
                return
            if network_grant["value"] is None:
                for key in ("token", "code"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        network_grant["value"] = val.strip()
                        break
        except Exception:
            pass

    async def _enable_network_capture() -> None:
        if not capture_callback:
            return
        try:
            await page.send(cdp.network.enable())
            page.add_handler(cdp.network.ResponseReceived, _on_response)
        except Exception as exc:
            print(f"[solver] network capture non disponibile: {exc}")

    async def _inject_widget() -> None:
        await page.evaluate(f"""
            (() => {{
                if (document.getElementById('_ts_box')) return;
                window._tsToken = null;
                const wrap = document.createElement('div');
                wrap.id = '_ts_box';
                wrap.style = 'position:fixed;top:20px;left:20px;z-index:2147483647;';
                document.body.appendChild(wrap);
                window._tsLoad = function () {{
                    turnstile.render('#_ts_box', {{
                        sitekey: '{sitekey}',
                        callback: function(token) {{ window._tsToken = token; }}
                    }});
                }};
                const s = document.createElement('script');
                s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_tsLoad&render=explicit';
                s.async = true;
                document.head.appendChild(s);
            }})();
        """)

    async def _open_fresh_page() -> None:
        """Chiude la pagina corrente (se presente) e ne apre una nuova
        sullo stesso siteurl — usato per il retry con reload."""
        nonlocal page
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        page = await browser.get(siteurl)
        await _try_hide_window()
        await _enable_network_capture()

    async def _try_hide_window() -> None:
        try:
            if hasattr(page, "minimize"):
                await page.minimize()
        except Exception:
            pass

    async def get_token() -> Optional[str]:
        return await page.evaluate("""
            (() => {
                if (window._tsToken) return window._tsToken;
                const inp = document.querySelector('#_ts_box [name="cf-turnstile-response"]');
                return (inp && inp.value) ? inp.value : null;
            })()
        """)

    async def get_current_url() -> str:
        return await page.evaluate("""
            (() => {
                try { return window.location.href || document.location.href || ''; }
                catch { return ''; }
            })()
        """)

    async def capture_callback_grant(
        current_url: Optional[str] = None,
    ) -> Optional[str]:
        nonlocal callback_grant
        if not capture_callback:
            return callback_grant
        if network_grant["value"]:
            callback_grant = network_grant["value"]
            return callback_grant
        url = current_url or await get_current_url()
        if not url:
            return callback_grant
        extracted = _extract_grant_from_callback_url(url)
        if extracted:
            callback_grant = extracted
        return callback_grant

    async def get_cf_iframe_rect() -> Optional[dict]:
        raw = await page.evaluate("""
            JSON.stringify((() => {
                for (const f of document.querySelectorAll('iframe')) {
                    const src = f.src || f.getAttribute('src') || '';
                    if (!src.includes('challenges.cloudflare.com')) continue;
                    const r = f.getBoundingClientRect();
                    if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
                }
                return null;
            })())
        """)
        if raw and raw != "null":
            return json.loads(raw)
        return None

    async def do_click(rect: Optional[dict]):
        if rect:
            cx = rect["x"] + 28 + random.uniform(-3, 3)
            cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
            print(f"[solver] clicking Cloudflare iframe at ({cx:.0f}, {cy:.0f})")
        else:
            cx = 20 + 28 + random.uniform(-3, 3)
            cy = 20 + 32 + random.uniform(-3, 3)
            print(
                f"[solver] iframe not in DOM, clicking fixed position ({cx:.0f}, {cy:.0f})"
            )
        await page.mouse_move(cx - 80, cy - 20)
        await asyncio.sleep(random.uniform(0.15, 0.25))
        await page.mouse_move(cx, cy)
        await asyncio.sleep(random.uniform(0.08, 0.15))
        await page.mouse_click(cx, cy)

    async def _try_solve_within(window_seconds: float) -> Optional[str]:
        """
        Tenta di ottenere il token entro `window_seconds`, cliccando la
        checkbox se necessario. In modalità capture_callback, considera
        "risolto" anche il solo ottenimento del grant di rete, anche senza
        un token esplicito (la pagina a volte non lo espone mai nel DOM).
        """
        token = await get_token()
        if token:
            return token
        if capture_callback:
            await capture_callback_grant()
            if callback_grant:
                return None  # grant già ottenuto, verificato dal chiamante

        rect = None
        for _ in range(20):
            rect = await get_cf_iframe_rect()
            if rect:
                break
            await asyncio.sleep(0.5)

        deadline = asyncio.get_event_loop().time() + window_seconds
        click_count = 0
        last_click = 0.0

        while asyncio.get_event_loop().time() < deadline:
            token = await get_token()
            if capture_callback:
                try:
                    await capture_callback_grant()
                    if callback_grant:
                        break
                except Exception:
                    pass
            if token:
                break

            now = asyncio.get_event_loop().time()
            if click_count == 0 or (not token and now - last_click > 8):
                if click_count >= 3:
                    await asyncio.sleep(0.3)
                    continue
                await do_click(rect)
                last_click = asyncio.get_event_loop().time()
                click_count += 1
                await asyncio.sleep(1.0)
                rect = await get_cf_iframe_rect() or rect
                continue

            await asyncio.sleep(0.3)

        return token

    token: Optional[str] = None
    per_attempt_seconds = (
        min(_RELOAD_CHECK_SECONDS, float(timeout)) if timeout else _RELOAD_CHECK_SECONDS
    )
    max_attempts = _MAX_RELOAD_ATTEMPTS

    try:
        page = await browser.get(siteurl)
        await _try_hide_window()
        await _enable_network_capture()
        await asyncio.sleep(random.uniform(2.0, 3.0))
        await _inject_widget()
        await asyncio.sleep(5.0)

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                print(
                    f"[solver] Nessun risultato entro {per_attempt_seconds:.0f}s, "
                    f"chiudo e riapro la pagina (tentativo {attempt}/{max_attempts})…"
                )
                await _open_fresh_page()
                await asyncio.sleep(random.uniform(2.0, 3.0))
                await _inject_widget()
                await asyncio.sleep(5.0)

            token = await _try_solve_within(per_attempt_seconds)

            if token or (capture_callback and callback_grant):
                break

            if attempt < max_attempts:
                print(
                    f"[solver] Tentativo {attempt}/{max_attempts} fallito, attendo 10s prima di riprovare…"
                )
                await asyncio.sleep(10.0)

        if token and hold_open_seconds > 0:
            await asyncio.sleep(hold_open_seconds)

        if capture_callback:
            try:
                await capture_callback_grant()
            except Exception:
                pass

    finally:
        browser.stop()

    if not token and not (capture_callback and callback_grant):
        raise TimeoutError(
            f"Turnstile token non ottenuto dopo {max_attempts} tentativi "
            f"({per_attempt_seconds:.0f}s ciascuno)"
        )

    return (token, callback_grant) if capture_callback else token


def _extract_grant_from_callback_url(callback_url: str) -> Optional[str]:
    if not callback_url:
        return None
    try:
        parsed = urlparse(callback_url)
    except Exception:
        return None

    for source in (parsed.query, parsed.fragment):
        if not source:
            continue
        query = dict(parse_qsl(source, keep_blank_values=True))
        grant = query.get("grant") or query.get("token") or query.get("code")
        if grant and grant.strip():
            return grant.strip()
    return None


_xvfb_lock = threading.Lock()
_xvfb_started = False


def _ensure_xvfb() -> None:
    """
    Starts a virtual display on headless Linux servers if one isn't already
    running. Previously this only happened in the __main__ CLI entry point,
    so any caller using solve()/solve_with_callback() as a library (e.g. the
    signed-session bridge) on a headless box without a DISPLAY would fail to
    launch Chrome. Idempotent and safe to call from multiple threads.
    """
    global _xvfb_started
    if _xvfb_started or platform.system() != "Linux" or os.environ.get("DISPLAY"):
        return
    with _xvfb_lock:
        if _xvfb_started or os.environ.get("DISPLAY"):
            return
        _start_xvfb_if_needed()
        _xvfb_started = True


def clear_solver_cache() -> None:
    _TURNSTILE_CACHE.clear()


def solve(
    sitekey: str, siteurl: str, timeout: int = 45, hold_open_seconds: float = 0.0
) -> str:
    import warnings

    _ensure_xvfb()

    cache_key = (sitekey.strip(), siteurl.strip())
    now = time.time()
    # hold_open_seconds keeps the browser tab open past the point of
    # getting a token, for callers whose target page does background work
    # after solving (e.g. calling its own /verify endpoint). That result
    # shouldn't be served from cache on a later call with hold_open_seconds
    # unset, so only use the cache for plain (hold_open_seconds == 0) calls.
    if hold_open_seconds <= 0:
        cached = _TURNSTILE_CACHE.get(cache_key)
        if cached is not None:
            cached_at, token = cached
            if now - cached_at <= DEFAULT_TURNSTILE_CACHE_TTL_SECONDS:
                return token
            _TURNSTILE_CACHE.pop(cache_key, None)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        token = asyncio.run(
            _solve_impl(sitekey, siteurl, timeout, hold_open_seconds=hold_open_seconds)
        )
    if hold_open_seconds <= 0:
        _TURNSTILE_CACHE[cache_key] = (now, token)
    return token


def solve_with_callback(
    sitekey: str, siteurl: str, timeout: int = 45, hold_open_seconds: float = 0.0
) -> tuple[str, Optional[str]]:
    import warnings

    _ensure_xvfb()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = asyncio.run(
            _solve_impl(
                sitekey,
                siteurl,
                timeout,
                capture_callback=True,
                hold_open_seconds=hold_open_seconds,
            )
        )

    if isinstance(result, tuple):
        return result
    return result, None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python solver.py <sitekey> <siteurl>")
        sys.exit(1)

    token = solve(sys.argv[1], sys.argv[2])
    print(token)

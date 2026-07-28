from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import random
import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qsl, urlparse

from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions
from pydoll.protocol.network.events import NetworkEvent

logger = logging.getLogger(__name__)

DEFAULT_TURNSTILE_CACHE_TTL_SECONDS = 900
_TURNSTILE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_RELOAD_CHECK_SECONDS = 10.0
_MAX_RELOAD_ATTEMPTS = 3


_docker_flags = []
if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
    _docker_flags = ["--no-sandbox", "--disable-dev-shm-usage"]


def _patch_nodriver_unknown_cdp_events() -> None:
    """No-op kept only for backward compatibility.

    This used to monkeypatch a nodriver bug where unknown/unrecognised CDP
    events raised a bare ``KeyError`` deep inside its connection loop.
    pydoll's connection layer does not have that issue, so there is nothing
    to patch anymore. The function is kept (as a no-op) purely because
    ``SpotiFLAC.core.signed_session_mono`` imports and calls it; removing it
    outright would break that import. New code should not call this.
    """
    return


logging.getLogger("asyncio").setLevel(logging.ERROR)


def _find_chrome() -> str:
    """Return the Chrome executable path, checking common locations per OS,
    including macOS and alternative Chromium-based browsers.
    """
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    if os.environ.get("BRAVE_PATH"):
        return os.environ["BRAVE_PATH"]

    system = platform.system()
    candidates: list[str] = []

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

    msg = (
        "No Chromium-based browser (Chrome, Edge, Brave, Arc) found on system. "
        "Install one of these browsers or set the CHROME_PATH environment variable."
    )
    raise FileNotFoundError(
        msg,
    )


def _get_profile_dir() -> str:
    """Return a persistent Chrome profile directory for the current OS."""
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    if platform.system() == "Windows":
        base = os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Temp"
        return os.path.join(base, "ts_profile")
    return "/tmp/ts_profile"


def _start_xvfb_if_needed() -> subprocess.Popen | None:
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


_xvfb_lock = threading.Lock()
_xvfb_started = False


def _ensure_xvfb() -> None:
    """Starts a virtual display on headless Linux servers if one isn't already
    running. Idempotent and safe to call from multiple threads.
    """
    global _xvfb_started
    if _xvfb_started or platform.system() != "Linux" or os.environ.get("DISPLAY"):
        return
    with _xvfb_lock:
        if _xvfb_started or os.environ.get("DISPLAY"):
            return
        _start_xvfb_if_needed()
        _xvfb_started = True


def build_chromium_options(*, hidden: bool = True) -> ChromiumOptions:
    """Build the ChromiumOptions used to launch the solver browser.

    Exposed (not prefixed with ``_``) so other modules that need to spin up
    a pydoll browser with the same persistent profile/flags (e.g.
    ``signed_session_mono``) don't have to duplicate this setup.
    """
    options = ChromiumOptions()
    options.binary_location = _find_chrome()
    options.headless = False
    # A persistent profile dir. pydoll doesn't have a first-class
    # `user_data_dir` option (yet), so it's passed as a raw Chromium flag,
    # same as nodriver did internally.
    options.add_argument(f"--user-data-dir={_get_profile_dir()}")
    options.add_argument("--window-size=1280,900")
    if hidden:
        # Push the (non-headless) window off-screen instead of using
        # --headless: a fully headless browser is more likely to be
        # challenged by Cloudflare than a real, visible-but-offscreen one.
        options.add_argument("--window-position=-32000,-32000")
    for flag in _docker_flags:
        options.add_argument(flag)
    return options


def _js_value(evaluate_response: dict):
    """Unwrap pydoll's raw CDP ``Runtime.evaluate`` response into the plain
    JS value.

    Unlike nodriver's ``page.evaluate()`` (which already returned the plain
    Python value), pydoll's ``tab.execute_script()`` returns the raw
    ``{"result": {"result": {"value": ...}}}`` CDP payload, so every call
    site needs to unwrap it. Always pair this with
    ``execute_script(..., return_by_value=True)`` so primitives/JSON come
    back as plain values instead of remote-object handles.
    """
    try:
        return evaluate_response["result"]["result"].get("value")
    except Exception:
        return None


def _extract_grant_from_callback_url(callback_url: str) -> str | None:
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


async def _solve_impl(
    sitekey: str,
    siteurl: str,
    timeout: int,
    capture_callback: bool = False,
    hold_open_seconds: float = 0.0,
) -> str | tuple[str, str | None]:
    options = build_chromium_options(hidden=True)
    browser = Chrome(options=options)
    tab = await browser.start()

    callback_grant = _extract_grant_from_callback_url(siteurl)
    network_grant: dict[str, str | None] = {"value": None}

    async def _on_response(event: dict) -> None:
        if not capture_callback:
            return
        try:
            params = event.get("params", {})
            response = params.get("response", {})
            mime = (response.get("mimeType") or "").lower()
            if "json" not in mime:
                return
            request_id = params.get("requestId")
            body = await tab.get_network_response_body(request_id)
            if not body:
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
            await tab.enable_network_events()
            await tab.on(NetworkEvent.RESPONSE_RECEIVED, _on_response)
        except Exception:
            pass

    async def _inject_widget() -> None:
        await tab.execute_script(
            f"""
            return (function () {{
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
        """Ricarica siteurl da zero — usato per il retry con reload.

        ``tab.go_to()`` in pydoll already refreshes when the URL matches the
        currently loaded page, so there's no need to close/reopen a tab like
        the old nodriver-based implementation did.
        """
        await tab.go_to(siteurl)
        await _enable_network_capture()

    async def get_token() -> str | None:
        response = await tab.execute_script(
            """
            return (function () {
                if (window._tsToken) return window._tsToken;
                const inp = document.querySelector('#_ts_box [name="cf-turnstile-response"]');
                return (inp && inp.value) ? inp.value : null;
            })();
        """,
            return_by_value=True,
        )
        return _js_value(response)

    async def get_current_url() -> str:
        response = await tab.execute_script(
            """
            return (function () {
                try { return window.location.href || document.location.href || ''; }
                catch (e) { return ''; }
            })();
        """,
            return_by_value=True,
        )
        return _js_value(response) or ""

    async def capture_callback_grant(
        current_url: str | None = None,
    ) -> str | None:
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

    async def get_cf_iframe_rect() -> dict | None:
        response = await tab.execute_script(
            """
            return JSON.stringify((function () {
                for (const f of document.querySelectorAll('iframe')) {
                    const src = f.src || f.getAttribute('src') || '';
                    if (!src.includes('challenges.cloudflare.com')) continue;
                    const r = f.getBoundingClientRect();
                    if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
                }
                return null;
            })());
        """,
            return_by_value=True,
        )
        raw = _js_value(response)
        if raw and raw != "null":
            return json.loads(raw)
        return None

    async def _has_native_widget() -> bool:
        rect = await get_cf_iframe_rect()
        return rect is not None

    async def _wait_for_native_widget(
        min_wait: float = 6.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """Aspetta fino a `min_wait` secondi che compaia il widget Turnstile
        NATIVO della pagina (quello che nel browser reale appare dopo ~5s
        di countdown), invece di iniettarne subito uno nostro. Ritorna True
        se il widget nativo è comparso, False se bisogna ricorrere al
        fallback di _inject_widget().
        """
        elapsed = 0.0
        while elapsed < min_wait:
            if await _has_native_widget():
                return True
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        return await _has_native_widget()

    async def do_click(rect: dict | None) -> None:
        if rect:
            cx = rect["x"] + 28 + random.uniform(-3, 3)
            cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
        else:
            cx = 20 + 28 + random.uniform(-3, 3)
            cy = 20 + 32 + random.uniform(-3, 3)
        await tab.mouse.move(cx - 80, cy - 20, humanize=True)
        await asyncio.sleep(random.uniform(0.15, 0.25))
        await tab.mouse.move(cx, cy, humanize=True)
        await asyncio.sleep(random.uniform(0.08, 0.15))
        await tab.mouse.click(cx, cy)

    async def _try_solve_within(window_seconds: float) -> str | None:
        """Tenta di ottenere il token entro `window_seconds`, cliccando la
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

    token: str | None = None
    per_attempt_seconds = (
        min(_RELOAD_CHECK_SECONDS, float(timeout)) if timeout else _RELOAD_CHECK_SECONDS
    )
    max_attempts = _MAX_RELOAD_ATTEMPTS

    try:
        await tab.go_to(siteurl)
        await _enable_network_capture()

        native_ready = await _wait_for_native_widget(min_wait=6.0)
        if not native_ready:
            await _inject_widget()
            await asyncio.sleep(2.0)

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                await _open_fresh_page()
                native_ready = await _wait_for_native_widget(min_wait=6.0)
                if not native_ready:
                    await _inject_widget()
                    await asyncio.sleep(2.0)

            token = await _try_solve_within(per_attempt_seconds)

            if token or (capture_callback and callback_grant):
                break

            if attempt < max_attempts:
                await asyncio.sleep(10.0)

        if token and hold_open_seconds > 0:
            await asyncio.sleep(hold_open_seconds)

        if capture_callback:
            with contextlib.suppress(Exception):
                await capture_callback_grant()

    finally:
        with contextlib.suppress(Exception):
            await browser.stop()

    if not token and not (capture_callback and callback_grant):
        msg = (
            f"Turnstile token non ottenuto dopo {max_attempts} tentativi "
            f"({per_attempt_seconds:.0f}s ciascuno)"
        )
        raise TimeoutError(
            msg,
        )

    return (token, callback_grant) if capture_callback else token


def clear_solver_cache() -> None:
    _TURNSTILE_CACHE.clear()


def solve(
    sitekey: str,
    siteurl: str,
    timeout: int = 45,
    hold_open_seconds: float = 0.0,
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
            _solve_impl(sitekey, siteurl, timeout, hold_open_seconds=hold_open_seconds),
        )
    if hold_open_seconds <= 0:
        _TURNSTILE_CACHE[cache_key] = (now, token)
    return token


def solve_with_callback(
    sitekey: str,
    siteurl: str,
    timeout: int = 45,
    hold_open_seconds: float = 0.0,
) -> tuple[str, str | None]:
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
            ),
        )

    if isinstance(result, tuple):
        return result
    return result, None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        sys.exit(1)

    token = solve(sys.argv[1], sys.argv[2])
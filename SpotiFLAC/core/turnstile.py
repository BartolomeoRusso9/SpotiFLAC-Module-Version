import asyncio
import json
import os
import platform
import random
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import parse_qsl, urlparse

DEFAULT_TURNSTILE_CACHE_TTL_SECONDS = 900
_TURNSTILE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}

import nodriver as uc


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
            "/usr/bin/microsoft-edge-stable"
        ]

    # 1. Controlla i percorsi standard
    for path in candidates:
        if os.path.exists(path):
            return path

    # 2. Ricerca dinamica nelle variabili d'ambiente globali (PATH)
    for cmd in ["google-chrome", "chrome", "chromium", "chromium-browser", "msedge", "brave"]:
        path = shutil.which(cmd)
        if path:
            return path

    raise FileNotFoundError(
        "Nessun browser basato su Chromium (Chrome, Edge, Brave, Arc) trovato nel sistema. "
        "Installa uno di questi browser oppure imposta la variabile CHROME_PATH."
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


async def _solve_impl(sitekey: str, siteurl: str, timeout: int, capture_callback: bool = False) -> str | tuple[str, Optional[str]]:
    browser = await uc.start(
        browser_executable_path=_find_chrome(),
        headless=False,
        user_data_dir=_get_profile_dir(),
    )

    page = None
    callback_grant = _extract_grant_from_callback_url(siteurl)

    try:
        page = await browser.get(siteurl)
        await asyncio.sleep(random.uniform(2.0, 3.0))

        # Inject widget into the live page DOM
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

        # Give Turnstile time to load and potentially auto-complete (invisible mode)
        await asyncio.sleep(5.0)

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

        async def capture_callback_grant(current_url: Optional[str] = None) -> Optional[str]:
            nonlocal callback_grant
            if not capture_callback:
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
            if raw and raw != 'null':
                return json.loads(raw)
            return None

        async def do_click(rect: Optional[dict]):
            if rect:
                cx = rect["x"] + 28 + random.uniform(-3, 3)
                cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                print(f"[solver] clicking Cloudflare iframe at ({cx:.0f}, {cy:.0f})")
            else:
                # Widget is fixed at top:20px left:20px
                cx = 20 + 28 + random.uniform(-3, 3)
                cy = 20 + 32 + random.uniform(-3, 3)
                print(f"[solver] iframe not in DOM, clicking fixed position ({cx:.0f}, {cy:.0f})")
            await page.mouse_move(cx - 80, cy - 20)
            await asyncio.sleep(random.uniform(0.15, 0.25))
            await page.mouse_move(cx, cy)
            await asyncio.sleep(random.uniform(0.08, 0.15))
            await page.mouse_click(cx, cy)

        # Check if already auto-solved (invisible widget)
        token = await get_token()
        if token:
            if not capture_callback:
                return token
            try:
                await capture_callback_grant()
            except Exception:
                pass
            if callback_grant:
                return (token, callback_grant)
            # We have a token but the challenge page hasn't redirected to
            # our cb URL with the grant yet - that redirect happens after
            # the page verifies the token server-side, which can take a
            # moment. Poll for it instead of giving up on the first check.
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    await capture_callback_grant()
                except Exception:
                    pass
                if callback_grant:
                    return (token, callback_grant)
                await asyncio.sleep(0.3)
            return (token, callback_grant)

        # Wait up to 10s for the visible checkbox iframe to appear
        rect = None
        for _ in range(20):
            rect = await get_cf_iframe_rect()
            if rect:
                break
            await asyncio.sleep(0.5)

        # Click loop: click, wait, retry up to 3 times
        deadline = asyncio.get_event_loop().time() + timeout
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
                # After a click, refresh iframe rect in case it moved
                await asyncio.sleep(1.0)
                rect = await get_cf_iframe_rect() or rect
                continue

            await asyncio.sleep(0.3)

    finally:
        browser.stop()

    # In capture_callback mode, a successful redirect to the callback URL
    # (i.e. callback_grant already captured) is a valid outcome even if the
    # Turnstile token itself was never read from the widget - the page may
    # have navigated away before get_token() ran again. Only raise if we
    # have neither a token nor a grant.
    if not token and not (capture_callback and callback_grant):
        raise TimeoutError(f"Turnstile token not obtained within {timeout}s")

    if capture_callback and page is not None:
        try:
            await capture_callback_grant()
        except Exception:
            pass

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


def solve(sitekey: str, siteurl: str, timeout: int = 45) -> str:
    import warnings

    _ensure_xvfb()

    cache_key = (sitekey.strip(), siteurl.strip())
    now = time.time()
    cached = _TURNSTILE_CACHE.get(cache_key)
    if cached is not None:
        cached_at, token = cached
        if now - cached_at <= DEFAULT_TURNSTILE_CACHE_TTL_SECONDS:
            return token
        _TURNSTILE_CACHE.pop(cache_key, None)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        token = asyncio.run(_solve_impl(sitekey, siteurl, timeout))
    _TURNSTILE_CACHE[cache_key] = (now, token)
    return token


def solve_with_callback(sitekey: str, siteurl: str, timeout: int = 45) -> tuple[str, Optional[str]]:
    import warnings

    _ensure_xvfb()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = asyncio.run(_solve_impl(sitekey, siteurl, timeout, capture_callback=True))

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
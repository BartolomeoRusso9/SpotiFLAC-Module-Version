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

# If set to "1", the browser window stays visible and is not moved off-screen
# or minimized. Useful for VNC debugging in Docker (see docker-entrypoint.sh + x11vnc).
# In production it should be left unset (or "0") so the behavior remains hidden.
_DEBUG_VISIBLE = os.environ.get("TS_DEBUG_VISIBLE", "").strip() == "1"


_docker_flags = ["--no-sandbox", "--disable-dev-shm-usage"]
_BROWSER_START_TIMEOUT_ENV = "TS_BROWSER_START_TIMEOUT"
_DEFAULT_BROWSER_START_TIMEOUT_SECONDS = 45


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


def _is_chromium_like(path: str) -> bool:
    """Heuristic: does this executable look like a Chromium-based browser?

    pydoll drives the browser over the Chrome DevTools Protocol, so only
    Chromium-based browsers (Chrome, Edge, Brave, Chromium, Opera, Vivaldi,
    Arc, ...) actually work. A system default browser that isn't
    Chromium-based (Firefox, Safari, ...) can't be used here.
    """
    name = os.path.basename(path).lower()
    keywords = (
        "chrome",
        "chromium",
        "msedge",
        "edge",
        "brave",
        "opera",
        "vivaldi",
        "arc",
    )
    return any(keyword in name for keyword in keywords)


def _default_browser_path_windows() -> str | None:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]

        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            rf"{prog_id}\shell\open\command",
        ) as key:
            command = winreg.QueryValueEx(key, "")[0]

        # command looks like: "C:\Path\To\browser.exe" -- %1
        import shlex

        parts = shlex.split(command, posix=False)
        if parts:
            return parts[0].strip('"')
    except Exception:
        return None
    return None


def _default_browser_path_macos() -> str | None:
    try:
        import plistlib

        ls_prefs_path = os.path.expanduser(
            "~/Library/Preferences/com.apple.LaunchServices/"
            "com.apple.launchservices.secure.plist",
        )
        if not os.path.exists(ls_prefs_path):
            return None

        with open(ls_prefs_path, "rb") as f:
            prefs = plistlib.load(f)

        bundle_id = None
        for handler in prefs.get("LSHandlers", []):
            if handler.get("LSHandlerURLScheme") == "http" and handler.get(
                "LSHandlerRoleAll",
            ):
                bundle_id = handler["LSHandlerRoleAll"]
                break
        if not bundle_id:
            return None

        app_path = (
            subprocess.run(
                ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            .stdout.strip()
            .splitlines()
        )
        if not app_path:
            return None

        app_bundle = app_path[0]
        macos_dir = os.path.join(app_bundle, "Contents", "MacOS")
        if not os.path.isdir(macos_dir):
            return None
        for entry in os.listdir(macos_dir):
            candidate = os.path.join(macos_dir, entry)
            if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
                return candidate
    except Exception:
        return None
    return None


def _default_browser_path_linux() -> str | None:
    try:
        desktop_name = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if not desktop_name:
            return None

        search_dirs = [
            os.path.expanduser("~/.local/share/applications"),
            "/usr/local/share/applications",
            "/usr/share/applications",
        ]
        desktop_file = None
        for directory in search_dirs:
            candidate = os.path.join(directory, desktop_name)
            if os.path.exists(candidate):
                desktop_file = candidate
                break
        if not desktop_file:
            return None

        with open(desktop_file, encoding="utf-8") as f:
            for line in f:
                if line.startswith("Exec="):
                    exec_line = line[len("Exec=") :].strip()
                    # Strip %u/%U/%f/%F/etc. placeholders and quoting.
                    import shlex

                    bin_name = shlex.split(exec_line)[0]
                    return shutil.which(bin_name) or bin_name
    except Exception:
        return None
    return None


def _get_default_browser_path() -> str | None:
    """Best-effort cross-platform lookup of the user's default web browser."""
    system = platform.system()
    if system == "Windows":
        return _default_browser_path_windows()
    if system == "Darwin":
        return _default_browser_path_macos()
    return _default_browser_path_linux()


def _find_chrome() -> str:
    """Return the Chrome executable path, checking common locations per OS,
    including macOS and alternative Chromium-based browsers.

    As a last resort, before giving up, this also checks the system's
    configured *default* browser: if the user's default browser happens to
    be Chromium-based (Chrome, Edge, Brave, Chromium, Opera, Vivaldi, Arc,
    ...) it's used, since pydoll can only drive Chromium-based browsers over
    the DevTools Protocol regardless of which one is "default".
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

    # 3. Fallback: usa il browser predefinito del sistema, se è
    # Chromium-based (altrimenti pydoll non potrebbe comunque pilotarlo
    # via CDP).
    default_path = _get_default_browser_path()
    if default_path and os.path.exists(default_path):
        if _is_chromium_like(default_path):
            logger.info(
                "[solver] Nessun Chrome/Chromium standard trovato: uso il "
                "browser predefinito del sistema (%s).",
                default_path,
            )
            return default_path
        logger.debug(
            "[solver] Il browser predefinito del sistema (%s) non è "
            "basato su Chromium: ignorato.",
            default_path,
        )

    msg = (
        "No Chromium-based browser (Chrome, Edge, Brave, Arc) found on system, "
        "and the system's default browser is not Chromium-based either. "
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

    Stealth configuration follows pydoll's own recommendations
    (https://pydoll.tech/docs/features/advanced/behavioral-captcha-bypass/):
    ``--disable-blink-features=AutomationControlled`` plus realistic
    ``browser_preferences`` that make the profile look like it's been used
    for a while, instead of a freshly-created automation profile.
    """
    # TS_DEBUG_VISIBLE=1 overrides `hidden`: keep the window on-screen and
    # normally positioned so it can be watched live via VNC.
    debug_visible = _DEBUG_VISIBLE

    options = ChromiumOptions()
    options.binary_location = _find_chrome()
    options.headless = False

    # pydoll uses a 10s default to verify the browser is alive after
    # launching the renderer process. We expose the same contract via env and
    # align the package's default with a 30s startup budget for Docker/CI, so
    # browser startup isn't immediately killed by the upstream watchdog.
    raw_start_timeout = os.environ.get(
        _BROWSER_START_TIMEOUT_ENV,
        str(_DEFAULT_BROWSER_START_TIMEOUT_SECONDS),
    ).strip()
    try:
        start_timeout_seconds = int(raw_start_timeout)
    except (TypeError, ValueError):
        start_timeout_seconds = _DEFAULT_BROWSER_START_TIMEOUT_SECONDS
    if start_timeout_seconds <= 0:
        start_timeout_seconds = _DEFAULT_BROWSER_START_TIMEOUT_SECONDS
    options.start_timeout = start_timeout_seconds

    profile_dir = _get_profile_dir()
    if os.path.exists(profile_dir):
        try:
            shutil.rmtree(profile_dir)
        except Exception:
            pass
    # A persistent profile dir. pydoll doesn't have a first-class
    # `user_data_dir` option (yet), so it's passed as a raw Chromium flag,
    # same as nodriver did internally.
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--window-size=1280,900")
    if hidden and not debug_visible:
        # Push the (non-headless) window off-screen instead of using
        # --headless: a fully headless browser is more likely to be
        # challenged by Cloudflare than a real, visible-but-offscreen one.
        options.add_argument("--window-position=-32000,-32000")
    for flag in _docker_flags:
        options.add_argument(flag)

    # --- Stealth: remove the most obvious automation signals ---------
    options.add_argument("--disable-blink-features=AutomationControlled")

    # A freshly-created profile (first launch, expires_at unset, etc.)
    # looks suspicious to fingerprinting. Pretend the profile has already
    # been used for a few hours and exited normally.
    current_time = int(time.time())
    options.browser_preferences = {
        "profile": {
            "last_engagement_time": str(current_time - (3 * 60 * 60)),
            "exited_cleanly": True,
            "exit_type": "Normal",
        },
        "safebrowsing": {"enabled": True},
    }

    logger.info(
        "[solver] Chromium launch options prepared: binary=%s start_timeout=%ss profile_dir=%s hidden=%s debug_visible=%s",
        options.binary_location,
        options.start_timeout,
        profile_dir,
        hidden,
        debug_visible,
    )

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


async def _try_minimize_window(browser: Chrome) -> None:
    """Best-effort: minimize the browser window to taskbar/dock.

    Combined with the off-screen ``--window-position`` flag, this keeps the
    solver browser fully out of the way even on systems/window managers
    where the off-screen trick alone isn't enough (e.g. some tiling WMs
    snap windows back on-screen). Failures are ignored: minimizing is a
    cosmetic nicety, not something the solve should fail over.

    Skipped entirely when TS_DEBUG_VISIBLE=1, so the window stays visible
    for VNC-based debugging.
    """
    if _DEBUG_VISIBLE:
        return
    try:
        await browser.set_window_minimized()
    except Exception as exc:
        logger.debug("[solver] could not minimize browser window: %s", exc)


def _describe_browser_start_error(
    exc: Exception,
    options: ChromiumOptions | None,
) -> str:
    binary = (
        getattr(options, "binary_location", None)
        or os.environ.get("CHROME_PATH")
        or os.environ.get("BRAVE_PATH")
        or "<unset>"
    )
    try:
        binary_exists = os.path.exists(binary)
    except Exception:
        binary_exists = False
    env_binary = (
        os.environ.get("CHROME_PATH") or os.environ.get("BRAVE_PATH") or "<unset>"
    )
    display = os.environ.get("DISPLAY") or "<unset>"
    profile_dir = _get_profile_dir()
    start_timeout = (
        getattr(options, "start_timeout", "n/a")
        if options is not None
        else "n/a"
    )
    return (
        "Browser failed to start inside pydoll/Chrome launch. "
        f"binary={binary!r} binary_exists={binary_exists} "
        f"configured_chrome_env={env_binary} display={display} "
        f"profile_dir={profile_dir} start_timeout={start_timeout}s "
        f"OS={platform.system()} message={exc!r}"
    )


async def _solve_impl(
    sitekey: str,
    siteurl: str,
    timeout: int,
    capture_callback: bool = False,
    hold_open_seconds: float = 0.0,
) -> str | tuple[str, str | None]:
    options: ChromiumOptions | None = None
    browser = None
    try:
        options = build_chromium_options(hidden=True)
    except Exception as exc:
        message = _describe_browser_start_error(exc, options)
        logger.error("[solver] %s", message)
        raise RuntimeError(
            "Browser failed to start. Verify the Chromium binary and Docker/host runtime; "
            "the pydoll startup watchdog timed out or the browser process never became discoverable. "
            "See the logs for the configured binary/profile/display details.",
        ) from exc
    try:
        logger.info(
            "[solver] launching browser through pydoll: binary=%s start_timeout=%ss",
            options.binary_location,
            getattr(options, "start_timeout", "n/a"),
        )
        browser = Chrome(options=options)
        tab = await browser.start()
    except Exception as exc:
        message = _describe_browser_start_error(exc, options)
        logger.error("[solver] %s", message)
        raise RuntimeError(
            "Browser failed to start. Verify the Chromium binary and Docker/host runtime; "
            "the pydoll startup watchdog timed out or the browser process never became discoverable. "
            "See the logs for the configured binary/profile/display details.",
        ) from exc
    await _try_minimize_window(browser)

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

    async def _navigate_with_turnstile_bypass() -> None:
        """Navigate to ``siteurl`` letting pydoll's native Turnstile helper
        handle the click for us (shadow-DOM traversal + realistic click).
        """
        # SPOSTATO IN CIMA: Abilita subito la cattura di rete per non perdersi l'auto-verifica!
        await _enable_network_capture()
        await _try_minimize_window(browser)

        async def _do_navigate():
            try:
                async with tab.expect_and_bypass_cloudflare_captcha(
                    time_before_click=random.uniform(1.0, 2.0),
                    time_to_wait_captcha=6, # Abbassiamo il timeout a 6s per non bloccarci troppo
                ):
                    await tab.go_to(siteurl)
            except AttributeError:
                # Older pydoll version without this helper
                await tab.go_to(siteurl)
            except Exception as exc:
                logger.debug(
                    "[solver] expect_and_bypass_cloudflare_captcha failed/skipped: %s",
                    exc,
                )

        # Eseguiamo la navigazione e la ricerca del captcha in un task separato (in background)
        nav_task = asyncio.create_task(_do_navigate())

        # Controlliamo costantemente se l'auto-verifica è andata a buon fine
        for _ in range(100):  # max 10 secondi
            if nav_task.done():
                break
            
            # Se la rete ha già catturato il grant, possiamo smettere di aspettare il click di pydoll
            if network_grant["value"]:
                logger.info("[solver] Grant catturato dalla rete! Interrompo l'attesa del bypass di pydoll.")
                break
                
            # Se la pagina mostra "Verified" (o lo status di successo), possiamo smettere di aspettare
            try:
                is_verified = await tab.execute_script(
                    "return document.body.innerText.includes('Verified') || document.querySelector('.status.success') !== null;",
                    return_by_value=True
                )
                if _js_value(is_verified):
                    logger.info("[solver] 'Verified' trovato nella pagina! Interrompo l'attesa del bypass di pydoll.")
                    break
            except Exception:
                pass

            await asyncio.sleep(0.1)

    async def _open_fresh_page() -> None:
        """Ricarica siteurl da zero — usato per il retry con reload."""
        await _navigate_with_turnstile_bypass()

    async def get_token() -> str | None:
        response = await tab.execute_script(
            """
            return (function () {
                if (window._tsToken) return window._tsToken;
                const inp = document.querySelector('#_ts_box [name="cf-turnstile-response"], [name="cf-turnstile-response"]');
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

    async def do_click(rect: dict | None) -> None:
        """Manual click fallback, kept for pydoll versions without native
        Turnstile support, or in case the native helper's single click
        wasn't enough (e.g. a second challenge appeared after reload).
        """
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
        """Tenta di ottenere il token entro `window_seconds`.

        The native Turnstile click already happened (if available) during
        `_navigate_with_turnstile_bypass()`/`_open_fresh_page()`, so this
        mostly polls for the resulting token/grant, and only falls back to
        manual clicking if nothing showed up yet.
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
        await _navigate_with_turnstile_bypass()

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                await _open_fresh_page()

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

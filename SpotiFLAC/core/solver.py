from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import random
import shutil
import signal
import subprocess
import tempfile
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
# Fixed per-attempt overhead the hard watchdog budget must also cover, on
# top of _watchdog_per_attempt (the token-polling window itself): the
# navigation-poll loop in _navigate_with_turnstile_bypass() (100 * 0.1s)
# and the iframe-rectangle discovery loop in _try_solve_within() (20 *
# 0.5s) each run once per attempt and can legitimately take their full
# duration before the token-polling window even starts.
_MAX_NAV_POLL_SECONDS = 100 * 0.1
_MAX_IFRAME_RECT_POLL_SECONDS = 20 * 0.5

# --- Global concurrency cap + hard watchdog -----------------------------
#
# Every caller (this module's own solve()/solve_with_callback(), the
# persistent browser in signed_session_mono.py, the per-verification
# thread in signed_session_desktop.py, the asyncio.to_thread() call in
# signed_session_mobile.py) spins up its own independent Chrome instance
# with no awareness of the others. Under heavy concurrent-download load
# that means an unbounded number of Chrome processes can pile up at once,
# each holding a chunk of RAM, until the host runs out of memory and the
# whole process gets killed by the OS OOM killer instead of failing
# gracefully.
#
# `_MAX_CONCURRENT_BROWSERS_ENV` puts a hard ceiling on that. It uses a
# plain `threading.Semaphore` (not `asyncio.Semaphore`) on purpose: every
# caller above runs its browser session on its *own* asyncio loop (either
# via its own `asyncio.run()` thread or `asyncio.to_thread()`), and an
# `asyncio.Semaphore` is only ever safe to share across a single loop.
# A `threading.Semaphore` is a real OS-level primitive that works
# correctly no matter how many independent event loops touch it.
_MAX_CONCURRENT_BROWSERS_ENV = "TS_MAX_CONCURRENT_BROWSERS"
_DEFAULT_MAX_CONCURRENT_BROWSERS = 3
_browser_slot_semaphore: threading.Semaphore | None = None
_browser_slot_semaphore_lock = threading.Lock()

# How long a caller is willing to wait for a free Chrome concurrency slot
# before giving up. Without a bound, a caller stuck behind a wedged/hung
# browser session that never releases its slot (see arm_hard_watchdog for
# why sessions can wedge) would block forever instead of surfacing a
# reportable error the caller can retry/log.
_BROWSER_SLOT_TIMEOUT_ENV = "TS_BROWSER_SLOT_TIMEOUT"
_DEFAULT_BROWSER_SLOT_TIMEOUT_SECONDS = 120.0


def _get_browser_slot_semaphore() -> threading.Semaphore:
    """Lazily create the process-wide Chrome concurrency semaphore.

    Sized once, on first use, from `TS_MAX_CONCURRENT_BROWSERS` (default 3).
    Kept as a single module-level singleton so it's genuinely shared by
    every caller in the process, regardless of which thread/event loop
    they're on.
    """
    global _browser_slot_semaphore
    if _browser_slot_semaphore is None:
        with _browser_slot_semaphore_lock:
            if _browser_slot_semaphore is None:
                raw = os.environ.get(
                    _MAX_CONCURRENT_BROWSERS_ENV,
                    str(_DEFAULT_MAX_CONCURRENT_BROWSERS),
                ).strip()
                try:
                    limit = int(raw)
                except (TypeError, ValueError):
                    limit = _DEFAULT_MAX_CONCURRENT_BROWSERS
                limit = max(1, limit)
                logger.info(
                    "[solver] global browser concurrency cap = %d (env %s)",
                    limit,
                    _MAX_CONCURRENT_BROWSERS_ENV,
                )
                _browser_slot_semaphore = threading.Semaphore(limit)
    return _browser_slot_semaphore


def _get_browser_slot_timeout_seconds() -> float:
    raw = os.environ.get(
        _BROWSER_SLOT_TIMEOUT_ENV, str(_DEFAULT_BROWSER_SLOT_TIMEOUT_SECONDS)
    ).strip()
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = _DEFAULT_BROWSER_SLOT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else _DEFAULT_BROWSER_SLOT_TIMEOUT_SECONDS


@contextlib.asynccontextmanager
async def acquire_browser_slot():
    """Blocks until a global Chrome concurrency slot is free, then holds it.

    Safe to call from any coroutine on any event loop/thread: acquiring
    the underlying `threading.Semaphore` is done via `asyncio.to_thread`
    so it never blocks the calling loop, only the worker thread the
    browser session is already running on.

    Bounded by `TS_BROWSER_SLOT_TIMEOUT` (default 120s): a caller stuck
    behind a wedged session that never releases its slot gets a reportable
    `TimeoutError` instead of hanging forever.
    """
    sem = _get_browser_slot_semaphore()
    timeout = _get_browser_slot_timeout_seconds()
    acquired = await asyncio.to_thread(sem.acquire, True, timeout)
    if not acquired:
        raise TimeoutError(
            f"Timed out after {timeout:.0f}s waiting for a free browser "
            f"concurrency slot (env {_BROWSER_SLOT_TIMEOUT_ENV})"
        )
    try:
        yield
    finally:
        sem.release()


def _kill_by_profile_dir(profile_dir: str) -> None:
    """Best-effort OS-level kill of every process tied to a Chrome profile
    dir. Last-resort cleanup shared by every module that manages its own
    pydoll browser session, used when a graceful `browser.stop()` failed
    or hung.
    """
    if not profile_dir:
        return
    with contextlib.suppress(Exception):
        if platform.system() != "Windows":
            # SIGTERM first, then SIGKILL — Chromium/Brave can be slow to act
            # on (or briefly ignore) SIGTERM, which on macOS left the window
            # visibly on screen. The profile dir is a throwaway, so a hard
            # kill is safe here.
            for sig in ("-TERM", "-KILL"):
                subprocess.run(
                    ["pkill", sig, "-f", profile_dir],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            # Windows: scope the kill to processes whose command line
            # references THIS solver's profile dir, so we never touch the
            # user's own browser windows. Works for whatever Chromium-based
            # exe pydoll launched (chrome.exe / msedge.exe / brave.exe /
            # chromium.exe).
            #
            # Preferred path is a PowerShell CIM query — `wmic` is
            # deprecated and absent on Windows 11 24H2+.
            ps_script = (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.CommandLine -like '*{profile_dir}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }"
            )
            killed = False
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        ps_script,
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
                killed = result.returncode == 0
            except Exception:
                killed = False

            if not killed:
                # Legacy fallback: wmic, still scoped to the profile dir and
                # to the known Chromium-based exe names. No unscoped
                # "/IM chrome.exe" kill — that would nuke the user's own
                # Chrome windows.
                matched_pids: list[str] = []
                try:
                    wmic_out = subprocess.run(
                        ["wmic", "process", "get", "ProcessId,CommandLine"],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=10,
                    )
                    for line in wmic_out.stdout.splitlines():
                        line = line.strip()
                        if not line or profile_dir not in line:
                            continue
                        pid = line.split()[-1]
                        if pid.isdigit():
                            matched_pids.append(pid)
                except Exception:
                    matched_pids = []

                for pid in matched_pids:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid, "/T"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )


def arm_hard_watchdog(browser, profile_dir: str | None, timeout_seconds: float):
    """Arms a background OS-thread timer that force-kills `browser`'s
    process after `timeout_seconds`, no matter what the asyncio side of
    the program is doing.

    Why this exists: a stuck CDP websocket, a renderer that stops
    responding, or any other kind of internal hang means an
    `asyncio.wait_for()`/cancellation-based timeout can itself get stuck —
    cancellation still has to propagate through an `await`, and if that
    await is the thing that's wedged (e.g. inside `browser.stop()` itself
    talking to a dead websocket), nothing ever unblocks. A
    `threading.Timer` runs on its own OS thread and can always deliver a
    real kill signal to the process by PID, independent of the event
    loop's state.

    Returns a zero-argument `cancel()` callable. Call it as soon as the
    guarded operation finishes normally so the timer doesn't fire later
    for no reason.
    """
    fired = threading.Event()

    def _kill() -> None:
        if fired.is_set():
            return
        fired.set()
        logger.warning(
            "[solver] hard watchdog fired after %.0fs — force-killing browser "
            "(profile_dir=%s)",
            timeout_seconds,
            profile_dir,
        )
        pid = None
        with contextlib.suppress(Exception):
            pid = browser._browser_process_manager._process.pid
        if pid:
            with contextlib.suppress(Exception):
                sig = (
                    signal.SIGTERM if platform.system() == "Windows" else signal.SIGKILL
                )
                os.kill(pid, sig)
        if profile_dir:
            _kill_by_profile_dir(profile_dir)

    timer = threading.Timer(timeout_seconds, _kill)
    timer.daemon = True
    timer.start()

    def _cancel() -> None:
        fired.set()  # blocks a race where the timer fires right after this check
        timer.cancel()

    return _cancel


# If set to "1", the browser window stays visible and is not moved off-screen
# or minimized. Useful for VNC debugging in Docker (see docker-entrypoint.sh + x11vnc).
# In production it should be left unset (or "0") so the behavior remains hidden.
_DEBUG_VISIBLE = os.environ.get("TS_DEBUG_VISIBLE", "").strip() == "1"


_docker_flags = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
]
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
                check=False,
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
            check=False,
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

    # 3. Fallback: use the system default browser if it is Chromium-based
    # (otherwise pydoll would not be able to drive it via CDP).
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
            "[solver] The system default browser (%s) is not "
            "Chromium-based: ignored.",
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
    """Return a persistent Chrome profile directory for the current OS, isolated per thread."""
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]

    if platform.system() == "Windows":
        base = os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Temp"
    else:
        base = "/tmp"

    # Use tempfile.mkdtemp to create a collision-resistant, unpredictable,
    # per-process/thread directory with restrictive permissions by default
    return tempfile.mkdtemp(prefix="ts_profile_", dir=base)


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


def build_chromium_options(*, hidden: bool = True) -> tuple[ChromiumOptions, str]:
    """Build the ChromiumOptions used to launch the solver browser.

    Exposed (not prefixed with ``_``) so other modules that need to spin up
    a pydoll browser with the same persistent profile/flags (e.g.
    ``signed_session_mono``) don't have to duplicate this setup.

    Stealth configuration follows pydoll's own recommendations
    (https://pydoll.tech/docs/features/advanced/behavioral-captcha-bypass/):
    ``--disable-blink-features=AutomationControlled`` plus realistic
    ``browser_preferences`` that make the profile look like it's been used
    for a while, instead of a freshly-created automation profile.

    Returns:
        A tuple of (ChromiumOptions, profile_dir) where profile_dir is the
        actual temp directory path created for this browser session.
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
            # FIX: If deletion fails because a zombie Chrome process
            # is holding the files locked, create a new folder on the fly to work around the lock (Errno 13).
            profile_dir = f"{profile_dir}_{int(time.time() * 1000)}"

    # A persistent profile dir. pydoll doesn't have a first-class...
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

    # Cloudflare Turnstile relies on rAF/timer-driven checks and on the page
    # being considered "visible" to actually register the checkbox
    # interaction. Chromium normally throttles/pauses timers and rendering
    # for occluded, minimized, or off-screen-positioned windows (which is
    # exactly how this browser is kept out of the user's way, see `hidden`
    # above and `_try_minimize_window()`), and that throttling is enough to
    # make the Turnstile click silently never register. These flags disable
    # that backgrounding behavior so the challenge keeps running normally
    # even while the window is minimized/off-screen. (Same fix already used
    # by ``signed_session_mono.py`` for its own browser session.)
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")

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

    return options, profile_dir


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
        getattr(options, "start_timeout", "n/a") if options is not None else "n/a"
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
    cancel_event: threading.Event | None = None,
    browser_info: dict | None = None,
) -> str | tuple[str, str | None]:
    options: ChromiumOptions | None = None
    browser = None
    profile_dir: str | None = None

    _hard_killed = {"done": False}

    class _SolverCancelled(Exception):
        """Raised to unwind _solve_impl immediately once the caller has
        cancelled — no further CDP calls against the (now dead) browser."""

    def _cancelled() -> bool:
        # The caller already has what it needs (e.g. the grant arrived on its
        # own local callback server) and wants this browser gone now.
        return cancel_event is not None and cancel_event.is_set()

    def _shutdown_now() -> None:
        """Synchronous, event-loop-independent teardown for the cancel path,
        then unwind via _SolverCancelled.

        `browser.stop()` is async and, once we're unwinding, may not get a
        chance to run; go straight to the OS-level kill scoped to this
        solver's own profile dir so the (visible, on macOS) window closes
        immediately instead of lingering until the hard watchdog. Raising
        afterwards stops any further CDP calls against the now-dead browser
        (which otherwise spin pydoll on "No healthy WebSocket connection").
        """
        if not _hard_killed["done"]:
            _hard_killed["done"] = True
            logger.info(
                "[solver] cancel requested — force-closing browser (profile_dir=%s)",
                profile_dir,
            )
            _kill_by_profile_dir(profile_dir)
        raise _SolverCancelled

    def cancel_watchdog() -> None:
        return None

    try:
        options, profile_dir = build_chromium_options(hidden=True)
        if browser_info is not None:
            # Let the caller kill this browser directly (OS-level, scoped to
            # this profile dir) if the cooperative cancel path can't run.
            browser_info["profile_dir"] = profile_dir
    except Exception as exc:
        message = _describe_browser_start_error(exc, options)
        logger.error("[solver] %s", message)
        raise RuntimeError(
            "Browser failed to start. Verify the Chromium binary and Docker/host runtime; "
            "the pydoll startup watchdog timed out or the browser process never became discoverable. "
            "See the logs for the configured binary/profile/display details.",
        ) from exc
    slot_cm = acquire_browser_slot()
    await slot_cm.__aenter__()
    try:
        logger.info(
            "[solver] launching browser through pydoll: binary=%s start_timeout=%ss",
            options.binary_location,
            getattr(options, "start_timeout", "n/a"),
        )
        browser = Chrome(options=options)
        tab = await browser.start()

        # Hard ceiling on the whole solve (all reload attempts + cleanup),
        # armed the moment we have a real PID to kill. Sized generously
        # above the normal solve budget so it never fires during legitimate
        # operation — it exists purely to guarantee this browser process
        # eventually dies even if something inside the CDP session wedges
        # completely (dead websocket, renderer that stops responding, ...).
        _watchdog_per_attempt = (
            min(_RELOAD_CHECK_SECONDS, float(timeout))
            if timeout
            else _RELOAD_CHECK_SECONDS
        )
        _watchdog_budget = (
            (
                _watchdog_per_attempt
                + _MAX_NAV_POLL_SECONDS
                + _MAX_IFRAME_RECT_POLL_SECONDS
            )
            * _MAX_RELOAD_ATTEMPTS
            + 10.0 * (_MAX_RELOAD_ATTEMPTS - 1)
            + max(0.0, hold_open_seconds)
            + 60.0  # buffer for browser.stop()/cleanup itself
        )
        cancel_watchdog = arm_hard_watchdog(browser, profile_dir, _watchdog_budget)
    except Exception as exc:
        message = _describe_browser_start_error(exc, options)
        logger.error("[solver] %s", message)
        # `browser` may already have a live subprocess behind it even though
        # `.start()` raised (e.g. the process spawned but pydoll's startup
        # watchdog timed out before confirming it). Best-effort close it so
        # we don't leave an orphaned Chrome window behind on every failed
        # start.
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.stop()
        with contextlib.suppress(Exception):
            await slot_cm.__aexit__(None, None, None)
        raise RuntimeError(
            "Browser failed to start. Verify the Chromium binary and Docker/host runtime; "
            "the pydoll startup watchdog timed out or the browser process never became discoverable. "
            "See the logs for the configured binary/profile/display details.",
        ) from exc
    # NOTE: the browser window is intentionally *not* minimized here anymore.
    # Minimizing (on top of the off-screen `--window-position`) makes
    # Chromium report `document.visibilityState === 'hidden'`, which
    # Cloudflare Turnstile treats as a signal to refuse to register the
    # checkbox interaction — the click silently never "takes". Staying
    # off-screen but not minimized keeps the browser out of the user's way
    # while the page still reports as visible, which is what Turnstile
    # actually requires to solve. `_try_minimize_window()` is kept for other
    # callers (e.g. ``signed_session_mono``) that don't need Turnstile to
    # keep working after the window is hidden.

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
                logger.debug("[solver:net] grant captured from the network")
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

    # Substrings from pydoll/CDP exceptions that mean the browser/tab itself
    # is gone (killed externally, websocket dropped, process died) rather
    # than a normal solve hiccup. Reload-retrying in that case is pointless
    # busywork — it just silently tries to "reopen" a browser that no
    # longer exists — so this is used to fail fast instead.
    _BROWSER_DEAD_MARKERS = (
        "closed",
        "disconnected",
        "connection is closed",
        "target closed",
        "no such execution context",
        "websocket",
    )

    def _looks_like_dead_browser(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _BROWSER_DEAD_MARKERS)

    async def _navigate_with_turnstile_bypass() -> None:
        """Navigate to ``siteurl`` letting pydoll's native Turnstile helper
        handle the click for us (shadow-DOM traversal + realistic click).
        """
        # MOVED UP: enable network capture immediately so we don't miss auto-verification!
        await _enable_network_capture()

        async def _do_navigate():
            try:
                async with tab.expect_and_bypass_cloudflare_captcha(
                    time_before_click=random.uniform(1.0, 2.0),
                    time_to_wait_captcha=6,  # Lower the timeout to 6s so we don't block too long
                ):
                    await tab.go_to(siteurl)
            except AttributeError:
                # Older pydoll version without this helper. Same dead-browser
                # handling as the native path below: a dead browser/tab must
                # still propagate instead of falling through to token-polling
                # against a browser that no longer exists, but any other
                # navigation hiccup is logged and swallowed.
                try:
                    await tab.go_to(siteurl)
                except Exception as exc:
                    if _looks_like_dead_browser(exc):
                        logger.warning(
                            "[solver] browser/tab appears to be closed, aborting navigate: %s",
                            exc,
                        )
                        raise
                    logger.debug(
                        "[solver] tab.go_to fallback failed/skipped: %s",
                        exc,
                    )
            except Exception as exc:
                if _looks_like_dead_browser(exc):
                    # Don't swallow this one: the browser/tab is actually
                    # gone (e.g. killed externally), so let it propagate
                    # instead of being treated as a normal skip-and-retry.
                    logger.warning(
                        "[solver] browser/tab appears to be closed, aborting navigate: %s",
                        exc,
                    )
                    raise
                logger.debug(
                    "[solver] expect_and_bypass_cloudflare_captcha failed/skipped: %s",
                    exc,
                )

        # Run navigation and captcha detection in a separate background task
        nav_task = asyncio.create_task(_do_navigate())
        # Never let an orphaned nav_task raise "exception never retrieved"
        # (it will, once the browser is killed on the cancel path).
        nav_task.add_done_callback(lambda t: t.cancelled() or t.exception())

        # Poll continuously to see whether auto-verification succeeded
        for _ in range(100):  # max 10 seconds
            if _cancelled():
                _shutdown_now()
            if nav_task.done():
                if nav_task.exception() is not None:
                    # Propagate a dead-browser failure immediately instead
                    # of falling through to the token-polling loop below,
                    # which would just hang/fail against a browser that no
                    # longer exists.
                    raise nav_task.exception()
                break

            # If the network already captured the grant, we can stop waiting for pydoll's click
            if network_grant["value"]:
                logger.info(
                    "[solver] Grant captured from the network! Stopping wait for pydoll bypass."
                )
                break

            # If the page shows "Verified" (or success status), we can stop waiting
            try:
                is_verified = await tab.execute_script(
                    "return document.body.innerText.includes('Verified') || document.querySelector('.status.success') !== null;",
                    return_by_value=True,
                )
                if _js_value(is_verified):
                    logger.info(
                        "[solver] 'Verified' found on the page! Stopping wait for pydoll bypass."
                    )
                    break
            except Exception:
                pass

            await asyncio.sleep(0.1)

        # We stopped polling (grant captured, "Verified", cancelled, or the
        # 10s ceiling). nav_task (pydoll's bypass context manager) is left
        # detached on purpose — cancel() would raise CancelledError up
        # through the finally and skip browser teardown; instead the browser
        # teardown/kill below makes its next CDP call fail. Its result is
        # already swallowed by the done-callback attached at creation.

    async def _open_fresh_page() -> None:
        """Reloads siteurl from scratch — used for retry with reload."""
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
        """Attempts to obtain the token within `window_seconds`.

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
                return None  # grant already obtained, verified by the caller

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
            if _cancelled():
                _shutdown_now()
                return token
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
            if _cancelled():
                _shutdown_now()
                break
            try:
                if attempt > 1:
                    await _open_fresh_page()

                token = await _try_solve_within(per_attempt_seconds)
            except Exception as exc:
                if _looks_like_dead_browser(exc):
                    # The browser/tab is gone — further reload attempts
                    # would just try to "reopen" a dead window. Stop
                    # retrying and fall through to cleanup/TimeoutError.
                    logger.warning(
                        "[solver] aborting reload attempts, browser/tab closed: %s",
                        exc,
                    )
                    break
                raise

            if token or (capture_callback and callback_grant):
                break

            if attempt < max_attempts:
                for _ in range(100):  # 10s, but bail out promptly if cancelled
                    if _cancelled():
                        _shutdown_now()
                        break
                    await asyncio.sleep(0.1)

        if token and hold_open_seconds > 0:
            await asyncio.sleep(hold_open_seconds)

        if capture_callback:
            with contextlib.suppress(Exception):
                await capture_callback_grant()

    except _SolverCancelled:
        logger.info("[solver] cancelled by caller — browser killed, unwinding")
        return ("", callback_grant) if capture_callback else ""

    finally:
        stopped_cleanly = _hard_killed["done"]
        if not stopped_cleanly:
            try:
                # Bounded wait: if browser.stop() itself hangs (e.g. the CDP
                # websocket is dead), don't block this coroutine forever —
                # fall through to the OS-level kill below. The hard watchdog
                # armed above is the final backstop in case even *this* await
                # never returns control.
                await asyncio.wait_for(browser.stop(), timeout=15.0)
                stopped_cleanly = True
            except Exception as exc:
                # "not running" / dead-websocket just means the browser was
                # already gone (e.g. killed on the cancel path, or the
                # challenge page closed its own tab) — expected, not a
                # problem. Only a genuine stop() failure is warn-worthy.
                already_gone = _looks_like_dead_browser(exc) or (
                    "not running" in str(exc).lower()
                )
                logger.log(
                    logging.DEBUG if already_gone else logging.WARNING,
                    "[solver] browser.stop() skipped/failed, forcing cleanup: %s",
                    exc,
                )
        if not stopped_cleanly:
            # Best-effort hard kill so a browser.stop() failure never leaves
            # the solver window open indefinitely (e.g. after the download
            # already finished). Scoped to this solver's own profile dir so
            # it doesn't touch unrelated Chrome windows the user has open.
            _kill_by_profile_dir(profile_dir)
        cancel_watchdog()
        with contextlib.suppress(Exception):
            await slot_cm.__aexit__(None, None, None)

    if not token and not (capture_callback and callback_grant):
        msg = (
            f"Turnstile token not obtained after {max_attempts} attempts "
            f"({per_attempt_seconds:.0f}s each)"
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
    cancel_event: threading.Event | None = None,
    browser_info: dict | None = None,
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
                cancel_event=cancel_event,
                browser_info=browser_info,
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

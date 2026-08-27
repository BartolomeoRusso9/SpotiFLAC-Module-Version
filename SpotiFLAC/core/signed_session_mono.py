"""Monochrome session handling (amz.geeked.wtf).

Because the backend verifies the JWT by tying it to the browser's TLS/network
fingerprint claim ('fp'), simply forwarding headers to httpx fails with 401.
The solution here keeps a persistent CDP browser (pydoll) in the background and
routes the GET /api/track/ request through `tab.request`, which performs the fetch
in the page's real JavaScript context — ensuring perfect fingerprint alignment,
just like a fetch() initiated manually from the page, but without having to
manage JSON (de)serialization ourselves.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import dataclasses
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from pydoll.browser.chromium import Chrome
from pydoll.protocol.network.events import NetworkEvent

from SpotiFLAC.core import get_amazon_endpoint
from SpotiFLAC.core.solver import (
    _ensure_xvfb,
    _kill_by_profile_dir,
    _try_minimize_window,
    acquire_browser_slot,
    build_chromium_options,
)

logger = logging.getLogger(__name__)

MONOCHROME_SESSION_SKEW = timedelta(minutes=2)
MONOCHROME_VERIFY_TIMEOUT = 60.0
MONOCHROME_PAGE_URL = "https://monochrome.tf/"
# Bound on browser.start() + initial navigation when spinning up the
# persistent mono browser, so a hang there can't hold the global browser
# slot (see acquire_browser_slot() below) forever.
MONOCHROME_BROWSER_START_TIMEOUT = 45.0


@dataclass
class MonochromeSessionRecord:
    jwt: str = ""
    expires_at: str = ""


def ensure_app_dir() -> str:
    app_dir = os.path.expanduser("~/.spotiflac")
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


def monochrome_session_path() -> str:
    directory = ensure_app_dir()
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)

    signed_sessions_dir = os.path.join(directory, "signed_sessions")
    os.makedirs(signed_sessions_dir, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(signed_sessions_dir, 0o700)

    return os.path.join(signed_sessions_dir, "monochrome_sessions.json")


def load_monochrome_session() -> MonochromeSessionRecord:
    path = monochrome_session_path()
    record = MonochromeSessionRecord()

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                valid_keys = {
                    f.name for f in dataclasses.fields(MonochromeSessionRecord)
                }
                filtered_data = {k: v for k, v in data.items() if k in valid_keys}
                record = MonochromeSessionRecord(**filtered_data)
        except Exception:
            pass

    return record


def save_monochrome_session(record: MonochromeSessionRecord) -> None:
    path = monochrome_session_path()
    data = json.dumps(asdict(record), indent=2)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(data)

    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)
    os.chmod(path, 0o600)


def _decode_jwt_exp(token: str) -> datetime | None:
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


class _MonochromeBrowserSession:
    """Keeps a persistent CDP browser (pydoll) around to route requests to
    the mono API (amz.geeked.wtf) INSIDE the browser's real TLS/fingerprint
    session, working around Cloudflare's WAF restrictions.
    """

    def __init__(self) -> None:
        self._browser: Chrome | None = None
        self._tab = None
        self._lock = asyncio.Lock()
        self._record = load_monochrome_session()
        self._ever_solved = False
        self._profile_dir: str | None = None
        # Holds the process-wide browser-slot semaphore (see solver.py) for
        # as long as this persistent browser is alive. Entered/exited
        # manually (not via `async with`) because the slot needs to stay
        # held across many fetch_track() calls, not just one.
        self._slot_cm = None

    async def _ensure_browser(self) -> None:
        if self._browser is not None and self._tab is not None:
            return
        _ensure_xvfb()

        options, profile_dir = build_chromium_options(hidden=False)
        options.add_argument("--incognito")

        # solver.py's own callers (solver.py itself, signed_session_desktop.py,
        # signed_session_mobile.py) all bound the number of concurrently-open
        # Chrome instances via this same semaphore (TS_MAX_CONCURRENT_BROWSERS,
        # default 3). This session used to spin up its own browser outside
        # that accounting, so under heavy concurrent download load it could
        # push the real number of live Chrome processes past the configured
        # cap, reintroducing the OOM risk the semaphore exists to prevent.
        slot_cm = acquire_browser_slot()
        await slot_cm.__aenter__()
        try:
            self._browser = Chrome(options=options)
            # Bounded: a hang here must not hold the slot forever.
            self._tab = await asyncio.wait_for(
                self._browser.start(),
                timeout=MONOCHROME_BROWSER_START_TIMEOUT,
            )
            await asyncio.wait_for(
                self._tab.go_to(MONOCHROME_PAGE_URL),
                timeout=MONOCHROME_BROWSER_START_TIMEOUT,
            )
            await _try_minimize_window(self._browser)
        except Exception:
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._browser.stop(), timeout=15.0)
            _kill_by_profile_dir(profile_dir)
            self._browser = None
            self._tab = None
            with contextlib.suppress(Exception):
                await slot_cm.__aexit__(None, None, None)
            raise

        self._profile_dir = profile_dir
        self._slot_cm = slot_cm

    async def _solve_turnstile_on_page(self, timeout: float) -> str:
        result: dict = {}

        async def _on_response(event: dict) -> None:
            if "access_token" in result:
                return
            try:
                params = event.get("params", {})
                response = params.get("response", {})
                if "auth/turnstile" not in (response.get("url") or ""):
                    return
                mime = (response.get("mimeType") or "").lower()
                if "json" not in mime:
                    return
                request_id = params.get("requestId")
                body = await self._tab.get_network_response_body(request_id)
                if not body:
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
            await self._tab.enable_network_events()
            await self._tab.on(NetworkEvent.RESPONSE_RECEIVED, _on_response)
        except Exception as exc:
            logger.debug("[monochrome] network capture unavailable: %s", exc)

        if self._ever_solved:
            try:
                await self._tab.refresh()
                await asyncio.sleep(1.0)
            except Exception:
                pass

        deadline = time.monotonic() + timeout
        reload_count = 0

        while "access_token" not in result and time.monotonic() < deadline:
            chunk_deadline = min(time.monotonic() + 10.0, deadline)

            while "access_token" not in result and time.monotonic() < chunk_deadline:
                await asyncio.sleep(0.5)

            if "access_token" not in result and time.monotonic() < deadline:
                if reload_count < 2:
                    reload_count += 1
                    logger.info(
                        f"[mono] No token received. Refreshing the page (attempt {reload_count}/2)...",
                    )
                    try:
                        await self._tab.refresh()
                        await asyncio.sleep(2.0)
                    except Exception:
                        pass
                else:
                    logger.warning("[mono] Max attempt, no token received.")
                    break

        if "access_token" not in result:
            msg = f"Timeout: no access_token JWT captured within {timeout:.0f}s"
            raise Exception(
                msg,
            )

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
        """Perform the GET routed through the tab's JS context in pydoll.

        `tab.request` performs HTTP calls directly in the browser's JavaScript
        context (the same principle as the manual fetch() previously used with
        nodriver), so it automatically inherits the tab's cookies/TLS/fingerprint
        — exactly what is needed here.
        """
        response = await self._tab.request.get(
            full_url,
            headers=[{"name": "X-Turnstile-JWT", "value": token}],
        )
        return {
            "ok": response.ok,
            "status": response.status_code,
            "body": response.text,
        }

    async def fetch_track(self, params: dict) -> dict:
        async with self._lock:
            return await self._fetch_track_with_restart(params, allow_restart=True)

    async def _fetch_track_with_restart(
        self,
        params: dict,
        *,
        allow_restart: bool,
    ) -> dict:
        await self._ensure_browser()
        token = await self._ensure_token()

        mono_url = get_amazon_endpoint("mono")
        from urllib.parse import urlencode

        qs = urlencode(params)
        sep = "&" if "?" in mono_url else "?"
        full_url = (
            f"{mono_url.rstrip('/') if '?' not in mono_url else mono_url}{sep}{qs}"
        )

        try:
            outer = await asyncio.wait_for(
                self._do_fetch(full_url, token),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[monochrome] track request timed out after 10s — restarting browser…",
            )
            await self._hard_reset()
            if not allow_restart:
                msg = "mono API request timed out after browser restart"
                raise RuntimeError(msg)
            return await self._fetch_track_with_restart(params, allow_restart=False)

        if not outer.get("ok") and outer.get("status") == 401:
            # Session invalidated server-side: force a new solve and retry ONCE.
            self._record = MonochromeSessionRecord()
            token = await self._ensure_token()
            try:
                outer = await asyncio.wait_for(
                    self._do_fetch(full_url, token),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[monochrome] retry after 401 also timed out — restarting browser…",
                )
                await self._hard_reset()
                if not allow_restart:
                    msg = "mono API request timed out after browser restart"
                    raise RuntimeError(
                        msg,
                    )
                return await self._fetch_track_with_restart(params, allow_restart=False)

        if not outer.get("ok"):
            msg = (
                f"mono API (in-browser) returned {outer.get('status')}: "
                f"{str(outer.get('body', ''))[:200]}"
            )
            raise RuntimeError(
                msg,
            )

        try:
            return json.loads(outer["body"])
        except Exception as exc:
            msg = f"mono API returned invalid JSON: {exc}"
            raise RuntimeError(msg) from exc

    async def _release_browser(self) -> None:
        """Stops the browser (best-effort, with a hard kill fallback) and
        releases the global browser-slot semaphore acquired in
        `_ensure_browser()`. Shared by `_hard_reset()` and `close()` so the
        slot is never left held by a browser that's no longer running.
        """
        if self._browser is not None:
            stopped_cleanly = False
            try:
                await asyncio.wait_for(self._browser.stop(), timeout=15.0)
                stopped_cleanly = True
            except Exception:
                pass
            if not stopped_cleanly and self._profile_dir:
                _kill_by_profile_dir(self._profile_dir)
        self._browser = None
        self._tab = None
        self._profile_dir = None
        if self._slot_cm is not None:
            with contextlib.suppress(Exception):
                await self._slot_cm.__aexit__(None, None, None)
            self._slot_cm = None

    async def _hard_reset(self) -> None:
        """Closes the browser and resets the token: forces a brand-new browser on the next attempt."""
        await self._release_browser()
        self._ever_solved = False
        self._record = MonochromeSessionRecord()

    async def close(self) -> None:
        async with self._lock:
            await self._release_browser()


_mono_browser_session: _MonochromeBrowserSession | None = None


def _get_mono_browser_session() -> _MonochromeBrowserSession:
    """Create the legacy browser helper on first use, never at import time."""
    global _mono_browser_session
    if _mono_browser_session is None:
        _mono_browser_session = _MonochromeBrowserSession()
    return _mono_browser_session


async def fetch_mono_track_via_browser(params: dict) -> dict:
    """Performs the GET /api/track/ routed inside the browser's CDP session."""
    return await _get_mono_browser_session().fetch_track(params)


async def close_mono_browser_session() -> None:
    if _mono_browser_session is not None:
        await _mono_browser_session.close()


def _close_mono_browser_sync() -> None:
    with contextlib.suppress(Exception):
        asyncio.run(close_mono_browser_session())


atexit.register(_close_mono_browser_sync)

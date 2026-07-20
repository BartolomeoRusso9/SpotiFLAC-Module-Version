"""
Gestione della sessione "Monochrome" (amz.geeked.wtf).

Poiché il backend verifica il JWT associandolo all'impronta TLS/di rete del browser 
(claim 'fp'), il semplice passaggio degli header a httpx fallisce con un 401. 
La soluzione implementata qui mantiene un browser CDP persistente in background e 
instrada la richiesta GET /api/track/ direttamente tramite la funzione fetch() 
nel contesto della pagina, garantendo il perfetto allineamento del fingerprint.
"""

import os
import json
import time
import asyncio
import base64
import dataclasses
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import logging
from urllib.parse import urlencode
import atexit

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

logger = logging.getLogger(__name__)

MONOCHROME_SESSION_SKEW = timedelta(minutes=2)
MONOCHROME_VERIFY_TIMEOUT = 60.0
MONOCHROME_PAGE_URL = "https://monochrome.tf/"

_patch_nodriver_unknown_cdp_events()

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
    """
    Mantiene un browser CDP persistente per instradare le richieste
    all'API mono (amz.geeked.wtf) DENTRO la sessione TLS/fingerprint reale
    del browser, aggirando le restrizioni WAF di Cloudflare.
    """

    def __init__(self) -> None:
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()
        self._record = load_monochrome_session()
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

        deadline = time.monotonic() + timeout
        reload_count = 0

        while "access_token" not in result and time.monotonic() < deadline:
            
            chunk_deadline = min(time.monotonic() + 10.0, deadline)
            
            while "access_token" not in result and time.monotonic() < chunk_deadline:
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
            f"""
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
            """,
            await_promise=True,
        )
        return json.loads(raw)

    async def fetch_track(self, params: dict) -> dict:
        async with self._lock:
            return await self._fetch_track_with_restart(params, allow_restart=True)

    async def _fetch_track_with_restart(
        self, params: dict, *, allow_restart: bool
    ) -> dict:
        await self._ensure_browser()
        token = await self._ensure_token()

        mono_url = get_amazon_endpoint("mono")
        qs = urlencode(params)
        sep = "&" if "?" in mono_url else "?"
        full_url = (
            f"{mono_url.rstrip('/') if '?' not in mono_url else mono_url}{sep}{qs}"
        )

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
                    raise RuntimeError(
                        "mono API request timed out after browser restart"
                    )
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
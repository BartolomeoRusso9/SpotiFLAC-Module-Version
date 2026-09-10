# SpotiFLAC/core/signed_session_mobile.py

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from .signed_session_errors import parse_session_error, should_clear_session

logger = logging.getLogger(__name__)

#: After a refresh the gateway refused (or never answered), how long before
#: the next request tries again. Without it, every signed request made after
#: `refresh_after` repeats the same refused refresh — one extra round trip per
#: track for the up-to-two hours until the session expires.
_REFRESH_RETRY_S = 300.0

#: Session file path -> monotonic time before which no refresh is attempted.
#: Module-level because runtime_features.signed_fetch() builds a new client
#: for every request, so nothing kept on an instance survives to the next one.
_REFRESH_RETRY_AT: dict[str, float] = {}


_DEFAULT_ENDPOINTS = {
    "bootstrap": "/bootstrap",
    "challenge": "/challenge",
    "exchange": "/session/exchange",
    # NOTE: no default for "refresh": the reference Go backend
    # (extension_signed_session.go) does not define one and only attempts
    # refresh if the manifest explicitly declares it in endpoints.refresh.
    # Guessing a default here risks hitting a nonexistent path when the
    # manifest does not declare refresh.
}

# Headers "from real browser" observed via DevTools on a call
# to a successful POST at {base_url}/challenge/verify (Brave on macOS, Chromium 149).
# Cloudflare/the API apply fingerprint verification on these headers:
# without them the request returns "Invalid request" even with a valid
# Turnstile token. Origin must be calculated per-instance (depends on base_url)
# and is added in __init__, not here.
_BROWSER_FINGERPRINT_HEADERS = {
    # Use a minimal, mobile-extension-friendly fingerprint observed in the
    # Qobuz extension captures: prefer JSON responses and gzip encoding.
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
}

_LOCAL_CALLBACK_HOST = "127.0.0.1"
_LOCAL_CALLBACK_PATH = "/callback"
_SOLVER_GRANT_TIMEOUT_S = 45  # solver timeout per attempt


async def _solver_grant_async(
    challenge_url: str, timeout: float = _SOLVER_GRANT_TIMEOUT_S
) -> str:
    """Call the external solver (turnstile-solver) to obtain a grant token.

    Used when stdin is not a TTY (Docker container without stdin_open).
    Reads TURNSTILE_SOLVER_URL from the environment.
    """
    solver_url = os.environ.get("TURNSTILE_SOLVER_URL", "").rstrip("/")
    if not solver_url:
        raise RuntimeError(
            "TURNSTILE_SOLVER_URL is not set, cannot auto-solve challenge"
        )
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        resp = await client.post(
            f"{solver_url}/grant",
            json={"challenge_url": challenge_url},
        )
        resp.raise_for_status()
        data = resp.json()
        grant = (data.get("grant") or "").strip()
        if not grant:
            raise RuntimeError(
                f"solver returned no grant: {data.get('error', 'unknown')}"
            )
        return grant


class SignedSessionClient:
    def __init__(
        self,
        base_url: str,
        namespace: str,
        app_version: str = "1.0",
        platform: str = "python-client",
        scheme_label: str = "SPOTIFLAC-HMAC-V1",
        header_prefix: str = "X-Sig-",
        window_seconds: int = 300,
        endpoints: dict[str, str] | None = None,
        data_dir: str = "~/.spotiflac/signed_sessions",
        refresh_skew_seconds: int = 3600,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.app_version = app_version
        self.platform = platform
        self.scheme_label = scheme_label
        self.header_prefix = header_prefix
        self.window_seconds = window_seconds
        self.endpoints = {**_DEFAULT_ENDPOINTS, **(endpoints or {})}
        self.refresh_skew_seconds = refresh_skew_seconds
        self.data_dir = Path(os.path.expanduser(data_dir))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Unlike the previous suppress(OSError), a failure to lock this
        # directory down to owner-only access must not be swallowed: the
        # session file written into it holds a live HMAC secret (see
        # _save() below), so a permission failure here has to abort
        # persistence rather than silently leave the directory world/group
        # readable.
        os.chmod(self.data_dir, 0o700)
        self._path = self._session_path()
        # Origin is ONLY scheme://host (no path): as confirmed by
        # DevTools screenshot, for base_url "https://api.zarz.moe/v2"
        # browser sends "Origin: https://api.zarz.moe", not ".../v2".
        _parsed_base = urlparse(self.base_url)
        _origin = f"{_parsed_base.scheme}://{_parsed_base.netloc}"
        # Build headers per-instance so we can include the exact User-Agent
        # that identifies this runtime + extension (observed in captures).
        # Do NOT create the AsyncClient here: creating it inside __init__ may
        # bind internal resources to the currently-running event loop, which
        # can later be closed (asyncio.run) and cause "Event loop is closed"
        # errors. Create the client lazily on first use instead.
        self._client: httpx.AsyncClient | None = None
        self._client_headers = {
            **_BROWSER_FINGERPRINT_HEADERS,
            # Do not include Origin by default; observed captures omit it for
            # these signed requests coming from the extension runtime.
            "User-Agent": f"SpotiFLAC-Mobile/{self.app_version}",
        }
        self.pending_auth_url: str | None = None
        self.pending_sitekey: str | None = None
        self.pending_challenge_id: str | None = None
        self._load()

    def set_cf_clearance(self, cf_clearance: str) -> None:
        """Inietta il cookie `cf_clearance` di Cloudflare nel client httpx.

        This cookie is the one that appears in the DevTools screenshot in the
        browser's successful request to /challenge/verify — it is tied to the
        TLS session/fingerprint with which it was obtained (typically the same
        browser/CDP session that resolved the Turnstile), so it must NOT be
        hardcoded: it should be passed here as soon as available, right before
        calling verify_challenge().

        If the core.turnstile.solve() module is able to return both the page
        cookies (beyond just the token), pass them here, e.g.:

            token, cookies = await asyncio.to_thread(solve, ...)
            if cookies.get("cf_clearance"):
                client.set_cf_clearance(cookies["cf_clearance"])
        """
        if not cf_clearance:
            return
        if self._client is None:
            self._ensure_client()
        self._client.cookies.set(
            "cf_clearance",
            cf_clearance,
            domain=urlparse(self.base_url).hostname,
        )

    # ─────────────────────── persistence ──────────────────────

    def _session_path(self) -> Path:
        scope = f"{self.namespace}\n{self.base_url.lower()}\n{self.app_version.lower()}\n{self.platform.lower()}"
        h = hashlib.sha256(scope.encode()).hexdigest()[:16]
        return self.data_dir / f"{self.namespace}-{h}.json"

    def _load(self) -> None:
        record: dict = {}
        if self._path.exists():
            try:
                record = json.loads(self._path.read_text())
            except Exception:
                record = {}

        self.install_id = record.get("install_id") or secrets.token_hex(16)
        self.session_id = record.get("session_id")
        self.session_secret = record.get("session_secret")
        self.expires_at = record.get("expires_at")
        # Optional fields returned by bootstrap/exchange/refresh — verified
        # via real network capture (2026-07-12): the response from
        # POST .../session/exchange also includes "refresh_after" (timestamp
        # absolute, preferred over our calculated skew) and
        # "capabilities" (list of session permissions, e.g.
        # ["resolve", "metadata", "download_ticket"]).
        self.refresh_after = record.get("refresh_after")
        self.capabilities = record.get("capabilities", [])
        self._save()

    def _ensure_client(self) -> None:
        """Creates the httpx client lazily and removes the "Connection" header.

        NOTE: it's not enough to omit "Connection" from the dict passed to
        `httpx.AsyncClient(headers=...)`. The internal setter of
        `httpx.Client.headers` always constructs a set of defaults
        (Accept, Accept-Encoding, Connection: keep-alive, User-Agent) and then
        merges those provided by us: if we don't specify "Connection", the
        httpx default "keep-alive" remains regardless of any .pop() done on
        our dict BEFORE client creation. It must therefore be removed AFTER,
        acting directly on the Headers object already constructed by the client.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(headers=self._client_headers, http2=True)
            # Remove the header from the client's actual Headers object,
            # not from the input dict (which at this point has already been merged
            # with httpx's internal defaults).
            self._client.headers.pop("Connection", None)

    def _save(self) -> None:
        record = {
            "install_id": self.install_id,
            "session_id": self.session_id,
            "session_secret": self.session_secret,
            "expires_at": self.expires_at,
            "refresh_after": self.refresh_after,
            "capabilities": self.capabilities,
        }
        # session_secret is sensitive (it's used to HMAC-sign every
        # request — see _sign_headers): write it out with owner-only
        # permissions instead of the platform-default (world/group
        # readable on some setups), matching the other signed-session
        # stores (see signed_session_mono.save_monochrome_session).
        #
        # Write through a temp file created with 0o600 from the start (so
        # the secret is never briefly world/group readable) and atomically
        # replace the real target, so a crash mid-write can't leave a
        # truncated/corrupt session file in place.
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            os.chmod(tmp_name, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(record, indent=2))
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)
            raise

    def clear(self) -> None:
        self.session_id = None
        self.session_secret = None
        self.expires_at = None
        self.refresh_after = None
        self.capabilities = []
        self.pending_auth_url = None
        self.pending_sitekey = None
        self.pending_challenge_id = None
        self._save()

    @property
    def authenticated(self) -> bool:
        if not self.session_id or not self.session_secret:
            return False
        exp = self._parse_time(self.expires_at)
        return not (exp and datetime.now(timezone.utc) > exp)

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    # ─────────────────────── bootstrap / challenge ────────────

    async def bootstrap(self):
        """Starts (or resumes) the verification flow. Returns True if a
        session was obtained directly, or an auth URL string if a
        challenge (e.g. Turnstile) needs to be solved first.
        """
        if self.pending_auth_url:
            return self.pending_auth_url

        self._ensure_client()
        # 30, not 15. When the origin behind Cloudflare is down, the 522 takes
        # about 19.5s to come back — so a 15s read timeout always fired first
        # and turned a diagnosable "Error 522: connection timed out" into a
        # bare timeout with no message at all.
        resp = await self._client.get(
            f"{self.base_url}{self.endpoints['bootstrap']}",
            params={"install_id": self.install_id, "app_version": self.app_version},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("[signed_session:%s] bootstrap response: %s", self.namespace, data)
        if (
            data.get("session_id")
            and data.get("session_secret")
            and data.get("expires_at")
        ):
            self.session_id = data["session_id"]
            self.session_secret = data["session_secret"]
            self.expires_at = data["expires_at"]
            self.refresh_after = data.get("refresh_after")
            self.capabilities = data.get("capabilities", [])
            self._save()
            return True

        self.pending_sitekey = (
            data.get("sitekey")
            or data.get("turnstile_sitekey")
            or data.get("turnstile_site_key")
        )
        self.pending_challenge_id = data.get("challenge_id")
        auth_url = data.get("auth_url") or data.get("challenge_url")
        if not auth_url and data.get("challenge_id"):
            auth_url = self._build_challenge_url(data["challenge_id"])
        if auth_url and not self.pending_sitekey:
            self.pending_sitekey = await self._scrape_sitekey_from_page(auth_url)
        self.pending_auth_url = auth_url
        return self.pending_auth_url

    async def _scrape_sitekey_from_page(self, page_url: str) -> str | None:
        try:
            self._ensure_client()
            resp = await self._client.get(page_url, timeout=10, follow_redirects=True)
        except Exception as exc:
            logger.debug(
                "[signed_session:%s] sitekey scrape fetch failed: %s",
                self.namespace,
                exc,
            )
            return None
        if not resp.is_success:
            return None
        html = resp.text
        for pattern in (
            r'data-sitekey=["\']([0-9A-Za-z_-]{10,})["\']',
            r'[\'"]sitekey[\'"]\s*:\s*[\'"]([0-9A-Za-z_-]{10,})[\'"]',
            r"sitekey=([0-9A-Za-z_-]{10,})",
        ):
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None

    def _build_challenge_url(self, challenge_id: str) -> str:
        # DEPRECATED: this helper does NOT add any "cb" — was
        # based on the assumption (proven wrong, see verify_challenge
        # below) that the grant was obtained by calling {endpoints.challenge}/verify
        # directly. The real backend (extension_signed_session.go,
        # buildSignedSessionChallengeURL) ALWAYS adds a "cb" parameter
        # with the callback URL. For the correct flow see
        # _build_challenge_url_with_callback() + authenticate_with_browser().
        # This method remains only for best-effort sitekey scraping
        # within bootstrap() (see _scrape_sitekey_from_page), which today
        # is no longer needed (we no longer automate Turnstile resolution),
        # but it's harmless to leave it.
        parts = list(urlparse(f"{self.base_url}{self.endpoints['challenge']}"))
        query = dict(parse_qsl(parts[4]))
        query["id"] = challenge_id
        parts[4] = urlencode(query)
        return urlunparse(parts)

    def _build_challenge_url_with_callback(
        self,
        challenge_id: str,
        callback_url: str,
    ) -> str:
        """Replicates EXACTLY buildSignedSessionChallengeURL() from the Go backend
        (extension_signed_session.go):

          1. the callback receives cb_version=v2grant in its query string
             and state=<namespace> (in Go it's state=<extensionID>: here we use
             the client's namespace, since a Python instance serves only one
             "logical extension" at a time);
          2. the challenge page URL ({base}/challenge) receives
             id=<challenge_id> and cb=<full callback_url, urlencoded>.

        `callback_url` here is typically the one returned by
        _LocalGrantListener.start(), i.e. http://127.0.0.1:{port}/callback
        instead of the mobile scheme "spotiflac://session-grant" — the
        challenge page makes no distinction and still redirects to the provided
        "cb" with ?grant=... appended.
        """
        cb_parts = list(urlparse(callback_url))
        cb_query = dict(parse_qsl(cb_parts[4]))
        cb_query["cb_version"] = "v2grant"
        cb_query["state"] = self.namespace
        cb_parts[4] = urlencode(cb_query)
        full_callback = urlunparse(cb_parts)

        parts = list(urlparse(f"{self.base_url}{self.endpoints['challenge']}"))
        query = dict(parse_qsl(parts[4]))
        query["id"] = challenge_id
        query["cb"] = full_callback
        parts[4] = urlencode(query)
        return urlunparse(parts)

    async def authenticate_with_turnstile(
        self,
        timeout: float = 60,
        hold_open_seconds: float = 3.0,
    ) -> None:
        """Automatic authentication via a real browser (core.turnstile).

        UPDATE: turnstile.py now captures the grant directly from
        network traffic via CDP (the same technique as grant_token.py /
        capture_network — listening for JSON responses containing the
        "grant" field), instead of relying on the final redirect URL. This
        fixes the issue documented in _LocalGrantListener: the challenge page
        internally calls {endpoints.challenge}/verify with its own cookies
        (including cf_clearance) but NEVER navigates to the provided "cb",
        so URL extraction was nearly always empty. Now the JSON response from
        that call is read directly, without having to replicate it from Python
        or hope for a missing redirect.

        Steps:
        1. bootstrap() to obtain challenge_id + sitekey;
        2. build the challenge URL with the same "cb" as the manual flow
           (used only as a fallback, not as the primary mechanism);
        3. have the real browser solve the widget — the grant is captured in
           real time as soon as the page receives the /verify response
           (solve_with_callback());
        4. exchange the grant with exchange_grant(), as in the manual flow.
        """
        boot_result = await self.bootstrap()
        if boot_result is True:
            return  # session already obtained, no verification necessary

        if not self.pending_challenge_id or not self.pending_sitekey:
            msg = (
                "bootstrap() did not return challenge_id/sitekey: "
                "unable to drive Turnstile automatically."
            )
            raise RuntimeError(
                msg,
            )

        dummy_callback = f"http://{_LOCAL_CALLBACK_HOST}:1{_LOCAL_CALLBACK_PATH}"
        challenge_url = self._build_challenge_url_with_callback(
            self.pending_challenge_id,
            dummy_callback,
        )

        from .solver import solve_with_callback

        _token, grant = await asyncio.to_thread(
            solve_with_callback,
            self.pending_sitekey,
            challenge_url,
            int(timeout),
            hold_open_seconds,
        )

        if not grant:
            msg = (
                "Turnstile solved (token obtained) but no 'grant' was captured "
                "from either the network or the callback redirect. Try increasing "
                "hold_open_seconds to give the page more time to complete "
                "the internal verify()."
            )
            raise RuntimeError(
                msg,
            )

        await self.exchange_grant(grant)

    @staticmethod
    def _emit_verification_url(
        url: str,
        callback: Callable[[str], None] | None,
    ) -> None:
        """Makes the verification URL available to the caller without ever
        opening it automatically in a browser.

        - If `callback` is provided, the URL is passed to it (the caller
          decides what to do: webbrowser.open(), UI, notification, etc.).
        - Otherwise it is printed to stdout and logged at WARNING level,
          so it remains visible even with the default logging configuration
          (WARNING) used by SpotiFLAC(...).
        """
        if callback is not None:
            callback(url)
            return
        logger.warning("[signed_session] Verification required: %s", url)

    async def verify_challenge(self, challenge_id: str, turnstile_token: str) -> str:
        """DO NOT CALL THIS DIRECTLY from Python — kept only for reference.

        This endpoint DOES exist and is exactly this: POST
        {base_url}{endpoints.challenge}/verify with
        {"challenge_id": ..., "turnstile_token": ...}, response
        {"grant": "...", "expires_in": 60} — confirmed via DevTools on a
        real call to the challenge page (200 OK).

        The reason calling it ourselves from Python fails (400): that
        request, when made by the page, includes a `cf_clearance` cookie
        from Cloudflare tied to that specific browser tab/session (in addition
        to the fingerprint headers already set on self._client). A headless
        HTTP client like this cannot obtain that cookie: it requires
        actually solving the Cloudflare challenge in a real browser.

        The correct flow (see authenticate_with_manual_grant) does not replicate
        this call: let the PAGE ITSELF do it (in the browser,
        with its cookies), and you read/paste the result from the
        Network tab of DevTools.
        """
        self._ensure_client()
        resp = await self._client.post(
            f"{self.base_url}{self.endpoints['challenge']}/verify",
            json={"challenge_id": challenge_id, "turnstile_token": turnstile_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        grant = data.get("grant")
        if not grant:
            msg = f"challenge verify did not return a grant: {data}"
            raise RuntimeError(msg)
        return grant

    async def exchange_grant(self, grant: str) -> None:
        resolved_grant = (grant or "").strip()
        if not resolved_grant:
            msg = "exchange_grant called without a grant"
            raise RuntimeError(msg)

        payload = {
            "grant": resolved_grant,
            "install_id": self.install_id,
            "app_version": self.app_version,
            "platform": self.platform,
        }
        self._ensure_client()
        resp = await self._client.post(
            f"{self.base_url}{self.endpoints['exchange']}",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.session_id = data["session_id"]
        self.session_secret = data["session_secret"]
        self.expires_at = data["expires_at"]
        self.refresh_after = data.get("refresh_after")
        self.capabilities = data.get("capabilities", [])
        self.pending_auth_url = None
        self.pending_sitekey = None
        self.pending_challenge_id = None
        self._save()

    async def _refresh(self) -> None:
        """Extends the current session. Never raises.

        A refresh that fails leaves the session exactly as valid as it was
        until `expires_at`, so it must not take the request that triggered it
        down with it — before, a network error here propagated out of
        ensure_session() and failed a download that had a working session.
        """
        refresh_path = self.endpoints.get("refresh")
        if not refresh_path:
            return  # no refresh endpoint declared: behavior identical to Go
        retry_key = str(self._path)
        if time.monotonic() < _REFRESH_RETRY_AT.get(retry_key, 0.0):
            return
        # Serialised once, and these exact bytes are both hashed and sent. It
        # used to hash json.dumps(body) — '{"install_id": "…"}', with a space —
        # while sending json=body, which httpx 0.28 writes without one: the
        # Body-Sha256 in the signature never matched the body on the wire,
        # and whatever the gateway answered was dropped without a word. A
        # session that is never refreshed dies at its first expiry and costs
        # a new Turnstile challenge.
        payload = json.dumps(
            {"install_id": self.install_id}, separators=(",", ":")
        ).encode()
        headers = self._sign_headers("POST", refresh_path, payload)
        headers["Content-Type"] = "application/json"
        self._ensure_client()
        try:
            resp = await self._client.post(
                f"{self.base_url}{refresh_path}",
                content=payload,
                headers=headers,
                timeout=15,
            )
        except httpx.HTTPError as exc:
            _REFRESH_RETRY_AT[retry_key] = time.monotonic() + _REFRESH_RETRY_S
            logger.warning(
                "[signed_session:%s] Session refresh failed: %s",
                self.namespace,
                str(exc) or type(exc).__name__,
            )
            return
        if not resp.is_success:
            _REFRESH_RETRY_AT[retry_key] = time.monotonic() + _REFRESH_RETRY_S
            logger.warning(
                "[signed_session:%s] Session refresh refused — HTTP %d: %s",
                self.namespace,
                resp.status_code,
                resp.text[:200],
            )
            return
        _REFRESH_RETRY_AT.pop(retry_key, None)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.session_id = data.get("session_id", self.session_id)
        self.session_secret = data.get("session_secret", self.session_secret)
        self.expires_at = data.get("expires_at", self.expires_at)
        self.refresh_after = data.get("refresh_after", self.refresh_after)
        self.capabilities = data.get("capabilities", self.capabilities)
        self._save()
        logger.info(
            "[signed_session:%s] Session refreshed, valid until %s",
            self.namespace,
            self.expires_at,
        )

    async def ensure_session(self) -> None:
        if not self.session_id or not self.session_secret:
            msg = "not authenticated: call bootstrap()/exchange_grant() first"
            raise RuntimeError(
                msg,
            )

        exp = self._parse_time(self.expires_at)
        if exp:
            now = datetime.now(timezone.utc)
            if now > exp:
                self.clear()
                msg = "session expired"
                raise RuntimeError(msg)

            # Prefer "refresh_after" (absolute timestamp given by the server,
            # verified via real network capture: e.g., 2h before expires_at,
            # not 1h like our default skew) — more precise than the calculated
            # skew, which remains only a fallback if the server doesn't send it.
            refresh_at = self._parse_time(self.refresh_after)
            if refresh_at:
                if now >= refresh_at:
                    await self._refresh()
            elif (exp - now).total_seconds() <= self.refresh_skew_seconds:
                await self._refresh()

    # ─────────────────────── signing ──────────────────────────

    def _sign_headers(self, method: str, path: str, body: bytes) -> dict:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        nonce = secrets.token_hex(12)
        body_hash = hashlib.sha256(body).hexdigest()
        window = int(time.time() // self.window_seconds)

        rk_bytes = hmac.new(
            self.session_secret.encode(),
            f"{window}:{self.session_id}".encode(),
            hashlib.sha256,
        ).digest()

        rk_string = base64.urlsafe_b64encode(rk_bytes).rstrip(b"=").decode("utf-8")

        # --- CRITICAL FIX: THE PATH ---
        # Combine base_url (which contains /v2) and path (which contains /tickets)
        # and extract the final correct path that the server expects to sign.
        full_url = f"{self.base_url}{path}"
        parsed_path = urlparse(full_url).path

        signing_input = f"{self.scheme_label}\n{method}\n{parsed_path}\n\n{body_hash}\n{ts}\n{nonce}\n{self.session_id}\n{self.app_version}\n{self.platform}"

        sig = (
            base64.urlsafe_b64encode(
                hmac.new(
                    rk_string.encode("utf-8"),
                    signing_input.encode("utf-8"),
                    hashlib.sha256,
                ).digest(),
            )
            .rstrip(b"=")
            .decode("utf-8")
        )

        p = self.header_prefix
        return {
            f"{p}Session": self.session_id,
            f"{p}Timestamp": ts,
            f"{p}Nonce": nonce,
            f"{p}Body-Sha256": body_hash,
            f"{p}Signature": sig,
            f"{p}App-Version": self.app_version,
            f"{p}Platform": self.platform,
        }

    async def request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        extra_headers: dict | None = None,
    ) -> httpx.Response:
        """Send a signed HTTP request to the specified API path.

        Parameters
        ----------
                method (str): HTTP method to use.
                path (str): Relative API path.
                json_body (Any): JSON-serializable request body.
                extra_headers (dict | None): Additional headers to include or override.

        Returns
        -------
                httpx.Response: The server response.

        """
        await self.ensure_session()
        body = (
            json.dumps(json_body, separators=(",", ":")).encode()
            if json_body is not None
            else b""
        )
        headers = self._sign_headers(method.upper(), path, body)
        if body:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        self._ensure_client()
        # Log headers and body we're about to send to the server
        try:
            b_preview = body[:1024]
            try:
                b_text = b_preview.decode("utf-8")
            except Exception:
                b_text = repr(b_preview)
            logger.debug(
                "[signed_session:%s] OUT %s %s headers=%s body=%s",
                self.namespace,
                method,
                path,
                headers,
                b_text,
            )
        except Exception:
            pass
        resp = await self._client.request(
            method,
            f"{self.base_url}{path}",
            content=body,
            headers=headers,
            timeout=30,
        )
        with contextlib.suppress(Exception):
            logger.info(
                'HTTP Request: %s %s "HTTP/1.1 %s %s"',
                method,
                str(resp.url),
                resp.status_code,
                getattr(resp, "reason_phrase", ""),
            )
        # Only a 401 the *gateway* attributed to itself means the session is
        # dead. One the gateway forwarded from the provider — no
        # subscription, region-locked, their token expired — says nothing
        # about our session, and clearing it there costs the user a
        # Turnstile challenge for a track that was simply unavailable.
        # See core/signed_session_errors.py.
        if resp.status_code in (401, 428):
            body = b""
            with contextlib.suppress(Exception):
                body = resp.content
            if should_clear_session(resp.status_code, body):
                self.clear()
            else:
                logger.debug(
                    "[signed_session:%s] %s came from the provider, "
                    "keeping the session",
                    self.namespace,
                    resp.status_code,
                )
        return resp

    # ─────────────────────── ticket / download layer ──────────────────────
    #
    # Formula and structure verified by reading the real source code
    # of the tidal-web extension (index.js, signedTicket function) and
    # verified against a real network capture (2026-07-12):
    #   sha256("tid:track:530979474") == "a5f4aee7d242692d616b4210cd61c48933b..."
    # which is exactly the resource_hash observed in the real request.
    # This part is generic: applies to any provider (not just Tidal),
    # since it's the signedSession runtime that manages it, not the individual
    # provider — only what the provider puts as the body of the POST
    # /dl/{provider} (that is specific to the provider).

    @staticmethod
    def compute_resource_hash(
        provider: str,
        resource_id: str,
        resource_type: str = "track",
    ) -> str:
        """Calcola il resource_hash richiesto da POST /tickets, ESATTAMENTE come
        the JS of official extensions does (e.g., tidal-web/index.js,
        signedTicket()):

            sha256(f"{provider}:{resource_type}:{str(resource_id).lower()}")

        E.g., compute_resource_hash("tid", "530979474") for a Tidal track
        (the "type" default is "track", like in JS: `type || "track"`).
        """
        raw = f"{provider}:{resource_type}:{str(resource_id).lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def get_download_ticket(
        self,
        provider: str,
        resource_id: str,
        resource_type: str = "track",
        tickets_path: str = "/tickets",
    ) -> str:
        resource_hash = self.compute_resource_hash(provider, resource_id, resource_type)

        # A download that never starts almost always died here, and until
        # now it died quietly: the ticket step is a whole network round trip
        # with its own authentication, and the only trace of it was the
        # exception the caller turned into "download failed". These three
        # lines say which of the two it was — asked and refused, or asked
        # and answered — without needing DEBUG on.
        logger.info(
            "[signed_session:%s] Ticket requested: %s %s:%s",
            self.namespace,
            provider,
            resource_type,
            resource_id,
        )
        started = time.monotonic()
        resp = await self.request(
            "POST",
            tickets_path,
            json_body={
                "capability": "download_ticket",
                "provider": provider,
                "resource_hash": resource_hash,
            },
        )
        elapsed = time.monotonic() - started
        if resp.status_code >= 400:
            logger.warning(
                "[signed_session:%s] Ticket refused for %s:%s — HTTP %d after "
                "%.1fs: %s",
                self.namespace,
                resource_type,
                resource_id,
                resp.status_code,
                elapsed,
                resp.text[:200],
            )
        resp.raise_for_status()
        data = resp.json()

        ticket_id = str(data.get("ticket_id") or data.get("ticket") or "").strip()
        if not ticket_id:
            logger.warning(
                "[signed_session:%s] Ticket NOT returned for %s:%s — the "
                "response carried no ticket_id: %s",
                self.namespace,
                resource_type,
                resource_id,
                str(data)[:200],
            )
            msg = f"ticket response missing ticket_id: {data}"
            raise RuntimeError(msg)

        logger.info(
            "[signed_session:%s] Ticket granted for %s:%s in %.1fs",
            self.namespace,
            resource_type,
            resource_id,
            elapsed,
        )
        return ticket_id

    async def ticketed_request(
        self,
        provider: str,
        resource_id: str,
        dl_path: str,
        json_body: dict,
        resource_type: str = "track",
        tickets_path: str = "/tickets",
    ) -> httpx.Response:
        """Gets a ticket for (provider, resource_id) and uses it immediately
        for a signed POST to `dl_path`, adding the header
        "X-Zarz-Ticket: <ticket_id>" like the JS does (postDownloadAPI()):

            ticket_id = await get_download_ticket(provider, resource_id, resource_type)
            POST {dl_path} with json_body, extra header X-Zarz-Ticket=ticket_id

        The response and body of `json_body` remain specific to the individual
        provider (e.g., for Tidal: {"id": track_id, "quality": "LOSSLESS"},
        response {"data": {"manifest": ..., "audioQuality": ..., ...}} — a
        DASH/XML manifest to be further parsed in a way specific to
        Tidal, not generic) — this method handles only the
        "ticket + header" level, not the parsing of the result.
        """
        ticket_id = await self.get_download_ticket(
            provider,
            resource_id,
            resource_type,
            tickets_path=tickets_path,
        )
        return await self.request(
            "POST",
            dl_path,
            json_body=json_body,
            extra_headers={"X-Zarz-Ticket": ticket_id},
        )

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None


# --- Phase 4: authentication synchronization --------
#
# What has to be serialized: authenticating one namespace. It is slow (a
# Turnstile solve is seconds, sometimes tens of seconds) and wasteful to do
# twice, so parallel tracks that all find themselves unauthenticated must
# queue behind the first one rather than each open their own browser.
#
# The primitive has to be loop-agnostic, and that is the whole difficulty.
# These locks live in a module-level dict, so they outlive any single event
# loop, while their waiters do not: `--web` serves requests on the server's
# loop, and other entry points (a CLI download, a worker that calls
# asyncio.run() of its own) bring loops of their own. An asyncio.Lock
# created on one loop and awaited from another raises
# "... is bound to a different event loop" and the caller dies on the spot —
# which is exactly what happened to ext:qobuz-web under --web, on every
# signed request it made.
#
# Keying the dict by (loop, namespace) would silence that error and
# reintroduce the bug the lock exists to prevent: two loops would take two
# different locks and authenticate the same namespace twice, concurrently.
#
# So the lock is a threading.Lock — which belongs to no loop — acquired
# without blocking and awaited by polling (see _AsyncThreadLock). Polling is
# affordable precisely because this guards a rare, slow operation: the cost
# is one 50 ms sleep per waiter per poll, against an authentication measured
# in seconds.
_AUTH_LOCKS: dict[str, threading.Lock] = {}

#: Guards creation of the per-namespace locks above. Without it two threads
#: racing on a namespace that has no lock yet can each build one and take
#: their own, which is the same double-authentication this is meant to stop.
_AUTH_LOCKS_GUARD = threading.Lock()


def _get_auth_lock(namespace: str) -> threading.Lock:
    """Return the lock for the given namespace, creating it if absent."""
    with _AUTH_LOCKS_GUARD:
        lock = _AUTH_LOCKS.get(namespace)
        if lock is None:
            lock = threading.Lock()
            _AUTH_LOCKS[namespace] = lock
        return lock


class _AsyncThreadLock:
    """`async with` over a threading.Lock, without blocking the event loop.

    `threading.Lock.acquire()` would stall the whole loop — every other
    coroutine on it, including the downloads this one is competing with — so
    the lock is taken non-blockingly and the coroutine yields between
    attempts.
    """

    #: Long enough that a queue of waiters costs nothing measurable, short
    #: enough to be invisible next to the authentication being awaited.
    _POLL_INTERVAL_S = 0.05

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock

    async def __aenter__(self) -> _AsyncThreadLock:
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(self._POLL_INTERVAL_S)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._lock.release()


# --- Backing off after a failed authentication -----------------------------
#
# A failed verification used to cost nothing to repeat, and it was repeated
# for every signed request that followed: runtime_features.signed_fetch()
# builds a fresh client per request, so nothing remembered that the attempt
# before had just failed, and every track of an album — times every fallback
# provider, times each extension's own retries — went back to /bootstrap and
# opened another Chromium on another challenge. Against a gateway that counts
# challenges per address, that is how one failed solve becomes a block
# measured in hours.
#
# So a failure now pauses authentication for the whole namespace — every
# extension sharing it talks to the same gateway from the same address — for
# longer after each consecutive failure, and never for less than the gateway
# itself asked. It is kept on disk beside the sessions, because what it
# guards against (going back to a gateway that has already said no) should
# not start over just because the container restarted.

_AUTH_BACKOFF_BASE_S = 300
_AUTH_BACKOFF_FACTOR = 3
_AUTH_BACKOFF_MAX_S = 4 * 3600

#: A failure this long after the previous pause ended is a new problem, not
#: the next step of the old one, and starts the schedule from the bottom.
_AUTH_BACKOFF_FORGET_S = 24 * 3600


def _auth_backoff_path(client: SignedSessionClient) -> Path:
    # Leading dot: session files are "<namespace>-<hash>.json", and this must
    # never be mistaken for one.
    return client.data_dir / f".{client.namespace}.auth-backoff.json"


def _read_auth_backoff(client: SignedSessionClient) -> dict:
    try:
        data = json.loads(_auth_backoff_path(client).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def auth_backoff_remaining(client: SignedSessionClient) -> float:
    """Seconds before this namespace may try to authenticate again, or 0."""
    try:
        not_before = float(_read_auth_backoff(client).get("not_before") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, not_before - time.time())


def _record_auth_failure(client: SignedSessionClient, retry_after: int = 0) -> float:
    """Starts (or extends) the pause after a failed authentication.

    Returns its length in seconds.
    """
    now = time.time()
    record = _read_auth_backoff(client)
    try:
        failures = int(record.get("failures") or 0)
        previous_end = float(record.get("not_before") or 0)
    except (TypeError, ValueError):
        failures, previous_end = 0, 0.0
    if now - previous_end > _AUTH_BACKOFF_FORGET_S:
        failures = 0
    failures += 1
    delay = min(
        _AUTH_BACKOFF_BASE_S * _AUTH_BACKOFF_FACTOR ** (failures - 1),
        _AUTH_BACKOFF_MAX_S,
    )
    # Deliberately not capped: a gateway that says "come back in 17 hours"
    # means it, and every request before then only pushes the date back.
    delay = max(delay, retry_after)
    with contextlib.suppress(OSError):
        _auth_backoff_path(client).write_text(
            json.dumps({"failures": failures, "not_before": now + delay})
        )
    return delay


def _clear_auth_backoff(client: SignedSessionClient) -> None:
    with contextlib.suppress(OSError):
        _auth_backoff_path(client).unlink()


def _retry_after_from(exc: BaseException) -> int:
    """The wait a refusal asked for, in seconds, or 0.

    Read from the Retry-After header, else from the gateway's error envelope
    (see signed_session_errors). Only httpx.HTTPStatusError carries a
    response; anything else — a timeout, a solver that never got a token —
    has nothing to say about it.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return 0
    raw = (response.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return int(raw)
    with contextlib.suppress(Exception):
        return parse_session_error(response.content).retry_after_seconds
    return 0


def _format_pause(seconds: float) -> str:
    minutes = max(1, int(seconds + 59) // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


async def perform_signed_fetch(
    client: SignedSessionClient,
    method: str,
    path: str,
    body: Any,
    headers: dict | None,
    on_verification_url: Callable[[str], None] | None = None,
    grant_input: Callable[[], str] | None = None,
    timeout: float = 300,
    use_turnstile_browser: bool = True,
) -> dict:
    """Perform an authenticated signed request with automatic session recovery.

    Parameters
    ----------
        client (SignedSessionClient): Client used to authenticate and send the request.
        method (str): HTTP method.
        path (str): Request path.
        body (Any): JSON request body.
        headers (dict | None): Additional request headers.
        on_verification_url (Callable[[str], None] | None): Callback for manual verification URLs.
        grant_input (Callable[[], str] | None): Callback that supplies a manual grant.
        timeout (float): Maximum authentication time in seconds.
        use_turnstile_browser (bool): Whether to attempt automated Turnstile authentication.

    Returns
    -------
        dict: Response details, a verification URL when reauthentication is required, or an error message.

    """
    try:
        # If we're not authenticated, acquire the async Lock
        if not client.authenticated:
            async with _AsyncThreadLock(_get_auth_lock(client.namespace)):
                # DOUBLE-CHECK: once inside the lock, reload the data from disk.
                # If another track running in parallel just authenticated in our
                # place, we'll see the refreshed session and skip authenticating!
                client._load()

                if not client.authenticated:
                    if not use_turnstile_browser:
                        return {"error": "RuntimeError: turnstile automation disabled"}
                    # Checked here, under the lock and after _load(), so a
                    # session another process obtained in the meantime is
                    # used rather than refused. Debug, not warning: during a
                    # pause every track lands here, and the failure that
                    # started it has already been logged once, with the time.
                    remaining = auth_backoff_remaining(client)
                    if remaining > 0:
                        logger.debug(
                            "[signed_session:%s] verification paused, %s left",
                            client.namespace,
                            _format_pause(remaining),
                        )
                        return {
                            "error": (
                                "verification paused after a failed attempt — "
                                f"next try in {_format_pause(remaining)}"
                            )
                        }
                    try:
                        await client.authenticate_with_turnstile(
                            timeout=min(timeout, 90),
                        )
                    except Exception as exc:
                        # `str(exc)` is empty for httpx's timeout classes —
                        # `str(ReadTimeout(TimeoutError()))` is "" — which is
                        # what turned a dead endpoint into the unreadable
                        # "Turnstile automatico fallito ()". The type name is
                        # always there, so lead with it and fall back to it.
                        detail = str(exc) or type(exc).__name__
                        pause = _record_auth_failure(client, _retry_after_from(exc))
                        logger.warning(
                            "[signed_session:%s] Turnstile automatico fallito "
                            "(%s: %s) — no new verification for %s",
                            client.namespace,
                            type(exc).__name__,
                            detail,
                            _format_pause(pause),
                        )
                        return {"error": f"{type(exc).__name__}: {detail}"}
                    _clear_auth_backoff(client)

        # At this point the session is guaranteed for all parallel tracks
        #
        # An extension's own ticket call arrives here rather than through
        # get_download_ticket(): the JS does its own POST /tickets over
        # signedFetch. It is the step a stalled download is most often stuck
        # in, so it is named in the log instead of being one anonymous
        # signed request among many.
        is_ticket = "/tickets" in path
        # /dl is the request that actually fetches the audio, and the two
        # together — ticket, then fetch — are the whole visible shape of a
        # signed download. Naming them both at info level is what tells a
        # stalled run apart from a slow one without turning on debug and
        # drowning in httpcore frames.
        is_download = "/dl" in path
        if is_ticket:
            logger.info(
                "[signed_session:%s] Ticket requested by the extension (%s %s)",
                client.namespace,
                method,
                path,
            )
        elif is_download:
            logger.info(
                "[signed_session:%s] Fetching audio (%s %s)",
                client.namespace,
                method,
                path,
            )
        else:
            logger.debug(
                "[signed_session:%s] signedFetch %s %s",
                client.namespace,
                method,
                path,
            )

        started = time.monotonic()
        resp = await client.request(method, path, json_body=body, extra_headers=headers)
        elapsed = time.monotonic() - started

        if is_ticket:
            granted = False
            if 200 <= resp.status_code < 300:
                # The body is the extension's to parse; all this needs to
                # know is whether a ticket came back at all, so that "asked
                # and got nothing" stops looking like "asked and got a
                # ticket" in the log.
                with contextlib.suppress(Exception):
                    payload = resp.json()
                    granted = bool(
                        isinstance(payload, dict)
                        and (payload.get("ticket_id") or payload.get("ticket"))
                    )
            if granted:
                logger.info(
                    "[signed_session:%s] Ticket returned in %.1fs",
                    client.namespace,
                    elapsed,
                )
            else:
                logger.warning(
                    "[signed_session:%s] Ticket NOT returned — HTTP %d after "
                    "%.1fs: %s",
                    client.namespace,
                    resp.status_code,
                    elapsed,
                    resp.text[:200],
                )
        elif is_download:
            logger.info(
                "[signed_session:%s] Audio source responded HTTP %d in %.1fs",
                client.namespace,
                resp.status_code,
                elapsed,
            )

        # Same distinction as in request(): re-bootstrapping and demanding
        # verification are session decisions, and a provider's refusal is
        # not grounds for either.
        if resp.status_code in (401, 428) and should_clear_session(
            resp.status_code, getattr(resp, "content", b"")
        ):
            retry_auth_url = await client.bootstrap()
            if isinstance(retry_auth_url, str) and retry_auth_url:
                return {"needsVerification": True, "auth_url": retry_auth_url}

        retry_after = 0
        raw_retry_after = resp.headers.get("Retry-After", "").strip()
        if raw_retry_after.isdigit():
            retry_after = max(0, int(raw_retry_after))

        return {
            "statusCode": resp.status_code,
            "status": resp.status_code,
            "ok": 200 <= resp.status_code < 300,
            "url": str(resp.url),
            "body": resp.text,
            "headers": dict(resp.headers),
            "retryAfterSeconds": retry_after,
        }
    except Exception as exc:
        # "warning", not "debug": this is the end of the road for whatever
        # the extension was doing, and returning {"error": …} means the JS
        # side decides how loudly to fail. At debug the reason was invisible
        # at the default level, so a download that stopped here reported
        # only "download failed".
        logger.warning(
            "[signed_session:%s] signedFetch %s %s failed: %s",
            client.namespace,
            method,
            path,
            exc,
        )
        return {"error": str(exc)}


def client_from_manifest(
    manifest_block: dict,
    data_dir: str = "~/.spotiflac/signed_sessions",
) -> SignedSessionClient:
    """Builds a SignedSessionClient from an extension manifest's `signedSession` block."""
    return SignedSessionClient(
        base_url=manifest_block["baseUrl"],
        namespace=manifest_block["namespace"],
        app_version=manifest_block.get("appVersion", "1.0"),
        platform=manifest_block.get("platform", "extension"),
        scheme_label=manifest_block.get("schemeLabel", "SPOTIFLAC-HMAC-V1"),
        header_prefix=manifest_block.get("headerPrefix", "X-Sig-"),
        window_seconds=int(manifest_block.get("timeWindowSeconds", 300)),
        endpoints=manifest_block.get("endpoints"),
        data_dir=data_dir,
    )

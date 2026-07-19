# SpotiFLAC/core/signed_session_mobile.py

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, parse_qs, urlencode, urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)


def is_docker() -> bool:
    """
    Determine whether the process is running inside a Docker container.

    Returns:
        bool: `True` if Docker container indicators are detected, `False` otherwise.
    """
    cgroup_path = "/proc/1/cgroup"
    if os.path.exists("/.dockerenv"):
        return True
    if os.path.isfile(cgroup_path):
        try:
            with open(cgroup_path) as f:
                return any("docker" in line for line in f)
        except OSError:
            return False
    return False


_DEFAULT_ENDPOINTS = {
    "bootstrap": "/bootstrap",
    "challenge": "/challenge",
    "exchange": "/session/exchange",
    # NOTE: no default for "refresh": il backend Go di riferimento
    # (extension_signed_session.go) non ne mette uno e tenta il refresh
    # solo se il manifest lo dichiara esplicitamente in endpoints.refresh.
    # Un default indovinato qui rischierebbe di colpire un path
    # inesistente quando il manifest non lo dichiara.
}

# Headers "from real browser" observed via DevTools su una chiamata
# on a successful POST to {base_url}/challenge/verify (Brave on macOS, Chromium 149).
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
_MANUAL_GRANT_TIMEOUT_S = 300  # 5 minutes to paste the grant


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
        """
        Inietta il cookie `cf_clearance` di Cloudflare nel client httpx.

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
            "cf_clearance", cf_clearance, domain=urlparse(self.base_url).hostname
        )

    # ─────────────────────── persistence ──────────────────────

    def _session_path(self) -> Path:
        scope = "\n".join(
            [
                self.namespace,
                self.base_url.lower(),
                self.app_version.lower(),
                self.platform.lower(),
            ]
        )
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
        self._path.write_text(json.dumps(record, indent=2))

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
        if exp and datetime.now(timezone.utc) > exp:
            return False
        return True

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
        challenge (e.g. Turnstile) needs to be solved first."""
        if self.pending_auth_url:
            return self.pending_auth_url

        self._ensure_client()
        resp = await self._client.get(
            f"{self.base_url}{self.endpoints['bootstrap']}",
            params={"install_id": self.install_id, "app_version": self.app_version},
            timeout=15,
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
        self, challenge_id: str, callback_url: str
    ) -> str:
        """
        Replica ESATTAMENTE buildSignedSessionChallengeURL() del backend Go
        (extension_signed_session.go):

          1. il callback riceve, nella propria query string, cb_version=v2grant
             and state=<namespace> (in Go it's state=<extensionID>: here we use the
             client's namespace, since a Python instance serves only one
             "logical extension" at a time);
          2. l'URL della pagina di sfida ({base}/challenge) riceve
             id=<challenge_id> e cb=<callback_url completo, urlencoded>.

        `callback_url` here is typically the one returned by
        _LocalGrantListener.start(), i.e., http://127.0.0.1:{port}/callback
        al posto dello scheme mobile "spotiflac://session-grant" — la pagina
        di sfida non fa alcuna distinzione, fa comunque un redirect al "cb"
        fornito con ?grant=... aggiunto.
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

    async def authenticate_with_manual_grant(
        self,
        on_verification_url: "Callable[[str], None] | None" = None,
        grant_input: "Callable[[], str] | None" = None,
        timeout: float = _MANUAL_GRANT_TIMEOUT_S,
    ) -> None:
        """
        Fallback completamente manuale, senza Playwright: nessun browser viene
        aperto o automatizzato da qui.

        Usage:
          1. bootstrap() obtains a challenge_id.
          2. The challenge page URL is displayed (via
             on_verification_url, or printed/logged by default).
          3. YOU open that URL in any browser and solve the Turnstile.
          4. Open DevTools → Network tab → find the "verify" request (POST
             to .../challenge/verify) → Preview tab: there you find
             {"grant": "gr_...", "expires_in": 60}.
          5. Copy that "grant" value (without quotes) and paste it
             when requested (or pass it via `grant_input`).
          6. exchange_grant(grant) exchanges the grant for a real session.

        The grant has a short lifespan (~60s from the verify response): copy and
        paste it as quickly as possible.

        Parameters:
          on_verification_url – callback to display the URL (otherwise
              printed to stdout and logged as WARNING).
          grant_input – function with no arguments that returns the grant as
              string (useful for non-interactive/GUI integrations). If not
              provided, asks for the grant via terminal input().
          timeout – maximum seconds to wait for grant entry
              (default 5 minutes). Raises RuntimeError if it expires before
              you (or grant_input) provide a value. NOTE: if the wait is on
              terminal input(), the thread blocked on that call
              is not interrupted when the timeout expires (Python limitation:
              you can't cancel a blocking input()) — it remains
              waiting in background until you press Enter, but the
              function still returns with the timeout error as soon as
              it triggers, without waiting for it.
        """
        boot_result = await self.bootstrap()
        if boot_result is True:
            return  # session obtained directly, no verification needed

        if not self.pending_challenge_id:
            if boot_result:
                self._emit_verification_url(boot_result, on_verification_url)
            raise RuntimeError(
                "The server provided an auth_url without challenge_id: "
                "unable to build the challenge URL."
            )

        dummy_callback = f"http://{_LOCAL_CALLBACK_HOST}:1{_LOCAL_CALLBACK_PATH}"
        challenge_url = self._build_challenge_url_with_callback(
            self.pending_challenge_id, dummy_callback
        )
        self._emit_verification_url(challenge_url, on_verification_url)

        if grant_input is not None:
            grant_awaitable = asyncio.to_thread(grant_input)
        else:
            grant_awaitable = asyncio.to_thread(
                input,
                "\nIncolla qui il grant (da DevTools → Network → verify → "
                "Preview → field 'grant'): ",
            )

        try:
            grant = await asyncio.wait_for(grant_awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Timeout ({timeout}s) waiting for grant entry") from exc

        grant = grant.strip()
        if not grant:
            raise RuntimeError("No grant provided.")

        await self.exchange_grant(grant)

    async def authenticate_with_turnstile(
        self,
        timeout: float = 60,
        hold_open_seconds: float = 3.0,
    ) -> None:
        """
        Autenticazione automatica tramite browser reale (core.turnstile),
        alternativa non-interattiva a authenticate_with_manual_grant().

        AGGIORNAMENTO: turnstile.py ora cattura il grant direttamente dal
        traffico di rete via CDP (stessa tecnica di grant_token.py /
        capture_network — ascolto delle risposte JSON in cerca del campo
        "grant"), invece di affidarsi all'URL di redirect finale. Questo
        risolve il problema documentato in _LocalGrantListener: la pagina
        di sfida chiama internamente {endpoints.challenge}/verify coi propri
        cookie (cf_clearance incluso) ma NON naviga mai al "cb" fornito, quindi
        l'estrazione da URL restava quasi sempre vuota. Ora la risposta JSON
        di quella chiamata viene letta direttamente, senza serve replicarla
        da Python né sperare in un redirect che non arriva.

        Passi:
        1. bootstrap() per ottenere challenge_id + sitekey;
        2. costruire l'URL di sfida con lo stesso "cb" del flusso manuale
           (serve solo come fallback, non più come meccanismo primario);
        3. far risolvere il widget al browser reale — il grant viene
           catturato in tempo reale non appena la pagina riceve la risposta
           di /verify (solve_with_callback());
        4. scambiare il grant con exchange_grant(), come nel flusso manuale.
        """
        boot_result = await self.bootstrap()
        if boot_result is True:
            return  # sessione già ottenuta, nessuna verifica necessaria

        if not self.pending_challenge_id or not self.pending_sitekey:
            raise RuntimeError(
                "bootstrap() non ha restituito challenge_id/sitekey: "
                "impossibile guidare Turnstile automaticamente."
            )

        dummy_callback = f"http://{_LOCAL_CALLBACK_HOST}:1{_LOCAL_CALLBACK_PATH}"
        challenge_url = self._build_challenge_url_with_callback(
            self.pending_challenge_id, dummy_callback
        )

        from .solver import solve_with_callback

        token, grant = await asyncio.to_thread(
            solve_with_callback,
            self.pending_sitekey,
            challenge_url,
            int(timeout),
            hold_open_seconds,
        )

        if not grant:
            raise RuntimeError(
                "Turnstile risolto (token ottenuto) ma nessun 'grant' catturato "
                "né dalla rete né dal redirect di callback. Prova ad aumentare "
                "hold_open_seconds per dare tempo alla pagina di completare "
                "la verify() interna."
            )

        await self.exchange_grant(grant)

    @staticmethod
    def _emit_verification_url(
        url: str, callback: "Callable[[str], None] | None"
    ) -> None:
        """
        Rende disponibile l'URL di verifica al chiamante, senza mai aprirlo
        automaticamente in un browser.

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
        print(
            f"\n[SpotiFLAC] Verification required — open this link in the browser:\n  {url}\n"
        )

    async def verify_challenge(self, challenge_id: str, turnstile_token: str) -> str:
        """
        NON CHIAMARLO DIRETTAMENTE da Python — lasciato solo per riferimento.

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
            raise RuntimeError(f"challenge verify did not return a grant: {data}")
        return grant

    async def exchange_grant(self, grant: str) -> None:
        resolved_grant = (grant or "").strip()
        if not resolved_grant:
            raise RuntimeError("exchange_grant called without a grant")

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
        refresh_path = self.endpoints.get("refresh")
        if not refresh_path:
            return  # no refresh endpoint declared: behavior identical to Go
        body = {"install_id": self.install_id}
        headers = self._sign_headers("POST", refresh_path, json.dumps(body).encode())
        self._ensure_client()
        resp = await self._client.post(
            f"{self.base_url}{refresh_path}",
            json=body,
            headers=headers,
            timeout=15,
        )
        if resp.is_success:
            data = resp.json()
            self.session_id = data.get("session_id", self.session_id)
            self.session_secret = data.get("session_secret", self.session_secret)
            self.expires_at = data.get("expires_at", self.expires_at)
            self.refresh_after = data.get("refresh_after", self.refresh_after)
            self.capabilities = data.get("capabilities", self.capabilities)
            self._save()

    async def ensure_session(self) -> None:
        if not self.session_id or not self.session_secret:
            raise RuntimeError(
                "not authenticated: call bootstrap()/exchange_grant() first"
            )

        exp = self._parse_time(self.expires_at)
        if exp:
            now = datetime.now(timezone.utc)
            if now > exp:
                self.clear()
                raise RuntimeError("session expired")

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

        signing_input = "\n".join(
            [
                self.scheme_label,
                method,
                parsed_path,
                "",
                body_hash,
                ts,
                nonce,
                self.session_id,
                self.app_version,
                self.platform,
            ]
        )

        sig = (
            base64.urlsafe_b64encode(
                hmac.new(
                    rk_string.encode("utf-8"),
                    signing_input.encode("utf-8"),
                    hashlib.sha256,
                ).digest()
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
        """
        Send a signed HTTP request to the specified API path.

        Parameters:
                method (str): HTTP method to use.
                path (str): Relative API path.
                json_body (Any): JSON-serializable request body.
                extra_headers (dict | None): Additional headers to include or override.

        Returns:
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
        try:
            logger.info(
                'HTTP Request: %s %s "HTTP/1.1 %s %s"',
                method,
                str(resp.url),
                resp.status_code,
                getattr(resp, "reason_phrase", ""),
            )
        except Exception:
            pass
        if resp.status_code in (401, 428):
            self.clear()
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
        provider: str, resource_id: str, resource_type: str = "track"
    ) -> str:
        """
        Calcola il resource_hash richiesto da POST /tickets, ESATTAMENTE come
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

        resp = await self.request(
            "POST",
            tickets_path,
            json_body={
                "capability": "download_ticket",
                "provider": provider,
                "resource_hash": resource_hash,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        ticket_id = str(data.get("ticket_id") or data.get("ticket") or "").strip()
        if not ticket_id:
            raise RuntimeError(f"ticket response missing ticket_id: {data}")

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
        """
        Ottiene un ticket per (provider, resource_id) e lo usa immediatamente
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
            provider, resource_id, resource_type, tickets_path=tickets_path
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


# --- Fase 4: sincronizzazione dell'autenticazione (async-native) --------
#
# PRIMA: SpotiFLAC scaricava più tracce in parallelo tramite un
# ThreadPoolExecutor dove ogni thread eseguiva il proprio asyncio.run()
# (quindi ogni thread aveva un event loop DIVERSO). Un asyncio.Lock keyed
# per (loop, namespace) non basta in quel caso: due thread paralleli, ognuno
# col proprio loop, otterrebbero ciascuno un lock "vergine" e non si
# sincronizzerebbero affatto — da qui l'accrocchio _AsyncThreadLockCtx che
# avvolgeva un threading.Lock con polling asincrono.
#
# ORA: con un solo processo/thread e un solo event loop condiviso (nessun
# download avvia più il proprio asyncio.run()), un dict di asyncio.Lock()
# indicizzato per namespace è sufficiente e corretto: tutte le coroutine
# "in gara" per autenticare lo stesso namespace girano sullo stesso loop,
# quindi asyncio.Lock le serializza nativamente senza bisogno di polling
# né di primitive thread-safe.
_AUTH_LOCKS: dict[str, asyncio.Lock] = {}


def _get_auth_lock(namespace: str) -> asyncio.Lock:
    """Restituisce (creandolo se assente) l'asyncio.Lock per il namespace."""
    lock = _AUTH_LOCKS.get(namespace)
    if lock is None:
        lock = asyncio.Lock()
        _AUTH_LOCKS[namespace] = lock
    return lock


async def perform_signed_fetch(
    client: SignedSessionClient,
    method: str,
    path: str,
    body: Any,
    headers: dict | None,
    on_verification_url: "Callable[[str], None] | None" = None,
    grant_input: "Callable[[], str] | None" = None,
    timeout: float = _MANUAL_GRANT_TIMEOUT_S,
    use_turnstile_browser: bool = True,
) -> dict:
    """
    Perform an authenticated signed request with automatic session recovery.

    Parameters:
        client (SignedSessionClient): Client used to authenticate and send the request.
        method (str): HTTP method.
        path (str): Request path.
        body (Any): JSON request body.
        headers (dict | None): Additional request headers.
        on_verification_url (Callable[[str], None] | None): Callback for manual verification URLs.
        grant_input (Callable[[], str] | None): Callback that supplies a manual grant.
        timeout (float): Maximum authentication time in seconds.
        use_turnstile_browser (bool): Whether to attempt automated Turnstile authentication.

    Returns:
        dict: Response details, a verification URL when reauthentication is required, or an error message.
    """
    try:
        # Se non siamo autenticati, richiediamo il Lock asincrono
        if not client.authenticated:
            lock = _get_auth_lock(client.namespace)
            async with lock:
                # DOUBLE-CHECK: Una volta dentro al blocco, ricarichiamo i dati dal disco.
                # Se un altro brano in parallelo ha appena fatto l'accesso al posto nostro,
                # troveremo la sessione aggiornata e salteremo l'autenticazione!
                client._load()

                if not client.authenticated:
                    running_in_docker = is_docker()
                    try:
                        if running_in_docker:
                            raise RuntimeError(
                                "ambiente Docker rilevato: skip diretto al flusso "
                                "manuale (authenticate_with_turnstile richiede un "
                                "browser reale non disponibile/affidabile in container)"
                            )
                        elif use_turnstile_browser:
                            await client.authenticate_with_turnstile(
                                timeout=min(timeout, 90)
                            )
                        else:
                            raise RuntimeError("turnstile automation disabled")
                    except Exception as exc:
                        if running_in_docker:
                            logger.info(
                                "[signed_session:%s] Docker rilevato, uso il flusso "
                                "manuale (%s).",
                                client.namespace,
                                exc,
                            )
                        else:
                            logger.info(
                                "[signed_session:%s] Turnstile automatico fallito (%s), "
                                "fallback al flusso manuale.",
                                client.namespace,
                                exc,
                            )
                        try:
                            await client.authenticate_with_manual_grant(
                                on_verification_url=on_verification_url,
                                grant_input=grant_input,
                                timeout=timeout,
                            )
                        except Exception as exc2:
                            logger.warning(
                                "[signed_session:%s] Autenticazione manuale fallita: %s",
                                client.namespace,
                                exc2,
                            )
                            return {"error": str(exc2)}

        # A questo punto la sessione è garantita per tutte le tracce parallele
        resp = await client.request(method, path, json_body=body, extra_headers=headers)

        if resp.status_code in (401, 428):
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
        logger.debug(
            "[signed_session:%s] signedFetch failed: %s", client.namespace, exc
        )
        return {"error": str(exc)}


def client_from_manifest(
    manifest_block: dict, data_dir: str = "~/.spotiflac/signed_sessions"
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

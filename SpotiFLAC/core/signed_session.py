# SpotiFLAC/core/signed_session.py
"""
Generic HMAC "signed session" client, driven entirely by the `signedSession`
block of an extension's manifest.json:

    "signedSession": {
      "namespace": "zarz-v2",
      "baseUrl": "https://api.zarz.moe/v2",
      "appVersion": "amzn@2.2.0",
      "platform": "extension",
      "callbackUrl": "spotiflac://session-grant",
      "schemeLabel": "ZARZ-HMAC-V1",
      "headerPrefix": "X-Zarz-",
      "timeWindowSeconds": 300,
      "endpoints": {
        "bootstrap": "/bootstrap",
        "challenge": "/challenge",
        "exchange": "/session/exchange",
        "refresh": "/session/refresh"
      }
    }

This is the Python-side counterpart to the JS extension's `session.signedFetch`
global (see extensions/_bridge.js): the session secret never touches the JS
worker, only Python holds it and performs the actual signing + HTTP call.

NOTE: every network path (bootstrap/challenge/exchange/refresh) reads its
route from `endpoints`, with the historical hardcoded paths kept only as
fallback defaults. This matters because a previous ad-hoc port of this class
hardcoded the refresh path as "/refresh" while some deployments declare
"/session/refresh" in the manifest, silently breaking session renewal.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx

from .turnstile import _extract_grant_from_callback_url

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINTS = {
    "bootstrap": "/bootstrap",
    "challenge": "/challenge",
    "exchange": "/session/exchange",
    "refresh": "/refresh",
}

_pending_signed_session_grants: dict[str, str] = {}
_pending_signed_session_grants_lock = threading.Lock()


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
        callback_url: str = "spotiflac://session-grant",
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
        self.callback_url = callback_url
        self._default_callback_url = callback_url
        self.endpoints = {**_DEFAULT_ENDPOINTS, **(endpoints or {})}
        self.refresh_skew_seconds = refresh_skew_seconds
        self.data_dir = Path(os.path.expanduser(data_dir))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._session_path()
        self._client = httpx.AsyncClient()
        self.pending_auth_url: str | None = None
        self.pending_auth_callback_url: str | None = None
        self.pending_sitekey: str | None = None
        self._callback_server: ThreadingHTTPServer | None = None
        self._callback_thread: threading.Thread | None = None
        self._callback_url: str | None = None
        self._load()

    # ─────────────────────── persistence ──────────────────────

    def _session_path(self) -> Path:
        scope = "\n".join(
            [self.namespace, self.base_url.lower(), self.app_version.lower(), self.platform.lower()]
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
        self._save()

    def _save(self) -> None:
        record = {
            "install_id": self.install_id,
            "session_id": self.session_id,
            "session_secret": self.session_secret,
            "expires_at": self.expires_at,
        }
        self._path.write_text(json.dumps(record, indent=2))

    def clear(self) -> None:
        self.session_id = None
        self.session_secret = None
        self.expires_at = None
        self.pending_auth_url = None
        self.pending_auth_callback_url = None
        self.pending_sitekey = None
        self._stop_callback_listener()
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

    def _set_pending_grant(self, grant: str) -> None:
        grant = (grant or "").strip()
        if not grant:
            return
        with _pending_signed_session_grants_lock:
            _pending_signed_session_grants[self.namespace] = grant

    def _consume_pending_grant(self) -> str | None:
        with _pending_signed_session_grants_lock:
            grant = _pending_signed_session_grants.pop(self.namespace, "").strip()
            return grant or None

    # ─────────────────────── bootstrap / challenge ────────────

    async def bootstrap(self):
        """Starts (or resumes) the verification flow. Returns True if a
        session was obtained directly, or an auth URL string if a
        challenge (e.g. Turnstile) needs to be solved first."""
        if self.pending_auth_url:
            return self.pending_auth_url

        resp = await self._client.get(
            f"{self.base_url}{self.endpoints['bootstrap']}",
            params={"install_id": self.install_id, "app_version": self.app_version},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("session_id") and data.get("session_secret") and data.get("expires_at"):
            self.session_id = data["session_id"]
            self.session_secret = data["session_secret"]
            self.expires_at = data["expires_at"]
            self._save()
            return True

        self.pending_sitekey = data.get("sitekey") or data.get("turnstile_sitekey")
        auth_url = data.get("auth_url") or data.get("challenge_url")
        if not auth_url and data.get("challenge_id"):
            auth_url = self._build_challenge_url(data["challenge_id"])
        if auth_url:
            self.pending_auth_url = self._rewrite_auth_url_callback(auth_url)
            self.pending_auth_callback_url = self.callback_url
        else:
            self.pending_auth_url = None
            self.pending_auth_callback_url = None
        return self.pending_auth_url

    def _start_callback_listener(self) -> str:
        if self._callback_server is not None and self._callback_thread is not None:
            return self._callback_url or self.callback_url

        class _GrantHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = f"http://127.0.0.1{self.path}"
                parsed = urlparse(url)
                grant = _extract_grant_from_callback_url(url)
                if not grant:
                    grant = parsed.query or parsed.fragment
                if grant:
                    self.server.session_client._set_pending_grant(grant)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _GrantHandler)
        server.session_client = self
        self._callback_server = server
        self._callback_url = (
            f"http://127.0.0.1:{server.server_port}/grant"
            f"?cb_version=v2grant&state={quote(self.namespace)}"
        )
        self.callback_url = self._callback_url
        self._callback_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._callback_thread.start()
        return self._callback_url

    def _stop_callback_listener(self) -> None:
        if self._callback_server is not None:
            self._callback_server.shutdown()
            self._callback_server.server_close()
            self._callback_server = None
        if self._callback_thread is not None:
            self._callback_thread.join(timeout=2)
            self._callback_thread = None
        self._callback_url = None
        self.callback_url = self._default_callback_url

    def _rewrite_auth_url_callback(self, auth_url: str) -> str:
        if not auth_url:
            return auth_url
        self._start_callback_listener()
        parsed = urlparse(auth_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["cb"] = self.callback_url
        query.setdefault("cb_version", "v2grant")
        query.setdefault("state", self.namespace)
        parts = list(parsed)
        parts[4] = urlencode(query)
        return urlunparse(parts)

    def _build_challenge_url(self, challenge_id: str) -> str:
        self._start_callback_listener()

        cb_parts = list(urlparse(self.callback_url))
        cb_query = dict(parse_qsl(cb_parts[4]))
        cb_query.update({"cb_version": "v2grant", "state": self.namespace})
        cb_parts[4] = urlencode(cb_query)
        callback = urlunparse(cb_parts)

        parts = list(urlparse(f"{self.base_url}{self.endpoints['challenge']}"))
        query = dict(parse_qsl(parts[4]))
        query.update({"id": challenge_id, "cb": callback})
        parts[4] = urlencode(query)
        return urlunparse(parts)

    async def exchange_grant(self, grant: str | None = None) -> None:
        resolved_grant = (grant or "").strip()
        if not resolved_grant:
            resolved_grant = self._consume_pending_grant() or ""
        if not resolved_grant:
            raise RuntimeError("no pending grant")

        payload = {
            "grant": resolved_grant,
            "install_id": self.install_id,
            "app_version": self.app_version,
            "platform": self.platform,
        }
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
        self.pending_auth_url = None
        self.pending_auth_callback_url = None
        self.pending_sitekey = None
        self._save()

    async def _refresh(self) -> None:
        body = {"install_id": self.install_id}
        # Uses the manifest-declared refresh path (may be "/refresh" or
        # "/session/refresh" depending on the provider) instead of a
        # hardcoded route, so signing input and URL always agree.
        refresh_path = self.endpoints["refresh"]
        headers = self._sign_headers("POST", refresh_path, json.dumps(body).encode())
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
            self._save()

    async def ensure_session(self) -> None:
        if not self.session_id or not self.session_secret:
            raise RuntimeError("not authenticated: call bootstrap()/exchange_grant() first")

        exp = self._parse_time(self.expires_at)
        if exp:
            now = datetime.now(timezone.utc)
            if now > exp:
                self.clear()
                raise RuntimeError("session expired")
            if (exp - now).total_seconds() <= self.refresh_skew_seconds:
                await self._refresh()

    # ─────────────────────── signing ──────────────────────────

    def _sign_headers(self, method: str, path: str, body: bytes) -> dict:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        nonce = secrets.token_hex(12)
        body_hash = hashlib.sha256(body).hexdigest()
        window = int(time.time() // self.window_seconds)
        rolling_key = hmac.new(
            self.session_secret.encode(),
            f"{window}:{self.session_id}".encode(),
            hashlib.sha256,
        ).digest()
        signing_input = "\n".join(
            [
                self.scheme_label, method, path, "", body_hash, ts, nonce,
                self.session_id, self.app_version, self.platform,
            ]
        )
        sig = base64.urlsafe_b64encode(
            hmac.new(rolling_key, signing_input.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        p = self.header_prefix
        return {
            f"{p}Session": self.session_id,
            f"{p}Timestamp": ts,
            f"{p}Nonce": nonce,
            f"{p}Body-SHA256": body_hash,
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
        await self.ensure_session()
        body = json.dumps(json_body).encode() if json_body is not None else b""
        headers = self._sign_headers(method.upper(), path, body)
        if body:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        resp = await self._client.request(
            method, f"{self.base_url}{path}", content=body, headers=headers, timeout=30,
        )
        if resp.status_code in (401, 428):
            self.clear()
        return resp

    async def aclose(self) -> None:
        self._stop_callback_listener()
        await self._client.aclose()


async def perform_signed_fetch(
    client: SignedSessionClient,
    method: str,
    path: str,
    body: Any,
    headers: dict | None,
    turnstile_timeout: float = 60.0,
) -> dict:
    """
    High-level handler for a `session.signedFetch(method, path, body, headers)`
    call coming from the JS extension worker. Transparently bootstraps the
    session and solves the Turnstile challenge (via core.turnstile) the first
    time it's needed, then performs the signed request.

    Returns a JSON-serializable dict matching what index.js's signedJSON()
    expects: {"statusCode": int, "body": str} on success,
    {"needsVerification": True, "auth_url": str} if it could not be solved
    automatically, or {"error": str} on failure.
    """
    try:
        if not client.authenticated:
            auth_url = await client.bootstrap()
            if auth_url is True:
                pass  # session obtained directly, no challenge needed
            elif auth_url:
                grant = None
                if client.pending_sitekey:
                    try:
                        from .turnstile import solve_with_callback

                        _, grant = solve_with_callback(
                            sitekey=client.pending_sitekey,
                            siteurl=auth_url,
                            timeout=int(turnstile_timeout),
                        )
                    except Exception as exc:
                        logger.warning(
                            "[signed_session:%s] Turnstile auto-solve failed: %s",
                            client.namespace, exc,
                        )
                if grant:
                    await client.exchange_grant(grant)
                else:
                    # Could not solve automatically: surface the challenge to
                    # the caller instead of failing silently.
                    return {"needsVerification": True, "auth_url": auth_url}
            else:
                return {"error": "bootstrap did not return a session or a challenge"}

        resp = await client.request(method, path, json_body=body, extra_headers=headers)
        return {"statusCode": resp.status_code, "body": resp.text}
    except Exception as exc:
        logger.debug("[signed_session:%s] signedFetch failed: %s", client.namespace, exc)
        return {"error": str(exc)}


def client_from_manifest(manifest_block: dict, data_dir: str = "~/.spotiflac/signed_sessions") -> SignedSessionClient:
    """Builds a SignedSessionClient from an extension manifest's `signedSession` block."""
    return SignedSessionClient(
        base_url=manifest_block["baseUrl"],
        namespace=manifest_block["namespace"],
        app_version=manifest_block.get("appVersion", "1.0"),
        platform=manifest_block.get("platform", "extension"),
        scheme_label=manifest_block.get("schemeLabel", "SPOTIFLAC-HMAC-V1"),
        header_prefix=manifest_block.get("headerPrefix", "X-Sig-"),
        window_seconds=int(manifest_block.get("timeWindowSeconds", 300)),
        callback_url=manifest_block.get("callbackUrl", "spotiflac://session-grant"),
        endpoints=manifest_block.get("endpoints"),
        data_dir=data_dir,
    )
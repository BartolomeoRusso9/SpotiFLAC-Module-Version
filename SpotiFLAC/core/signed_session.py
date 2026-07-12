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
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINTS = {
    "bootstrap": "/bootstrap",
    "challenge": "/challenge",
    "exchange": "/session/exchange",
    # NOTE: nessun default per "refresh": il backend Go di riferimento
    # (extension_signed_session.go) non ne mette uno e tenta il refresh
    # solo se il manifest lo dichiara esplicitamente in endpoints.refresh.
    # Un default indovinato qui rischierebbe di colpire un path
    # inesistente quando il manifest non lo dichiara.
}

# Header "da browser reale" osservati via DevTools su una chiamata
# riuscita a POST {base_url}/challenge/verify (Brave su macOS, Chromium 149).
# Cloudflare/l'API applicano un controllo di fingerprint su questi header:
# senza di essi la richiesta torna "Invalid request" anche con un token
# Turnstile valido. Origin va calcolato per istanza (dipende da base_url)
# e viene aggiunto in __init__, non qui.
_BROWSER_FINGERPRINT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "it-IT,it;q=0.7",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-Ua": '"Brave";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Gpc": "1",
    "Priority": "u=1, i",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}


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
        # Origin è SOLO scheme://host (niente path): come confermato dallo
        # screenshot DevTools, per base_url "https://api.zarz.moe/v2" il
        # browser manda "Origin: https://api.zarz.moe", non ".../v2".
        _parsed_base = urlparse(self.base_url)
        _origin = f"{_parsed_base.scheme}://{_parsed_base.netloc}"
        self._client = httpx.AsyncClient(
            headers={
                **_BROWSER_FINGERPRINT_HEADERS,
                "Origin": _origin,
            }
        )
        self.pending_auth_url: str | None = None
        self.pending_sitekey: str | None = None
        self.pending_challenge_id: str | None = None
        self._load()

    def set_cf_clearance(self, cf_clearance: str) -> None:
        """
        Inietta il cookie `cf_clearance` di Cloudflare nel client httpx.

        Questo cookie è quello che nello screenshot DevTools compare nella
        richiesta riuscita del browser a /challenge/verify — è legato alla
        sessione/fingerprint TLS con cui è stato ottenuto (tipicamente la
        stessa sessione browser/CDP che ha risolto il Turnstile), quindi
        NON va hardcodato: va passato qui non appena disponibile, subito
        prima di chiamare verify_challenge().

        Se il modulo core.turnstile.solve() è in grado di restituire anche
        i cookie della pagina (oltre al solo token), passali qui, es.:

            token, cookies = await asyncio.to_thread(solve, ...)
            if cookies.get("cf_clearance"):
                client.set_cf_clearance(cookies["cf_clearance"])
        """
        if not cf_clearance:
            return
        self._client.cookies.set(
            "cf_clearance", cf_clearance, domain=urlparse(self.base_url).hostname
        )

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

        resp = await self._client.get(
            f"{self.base_url}{self.endpoints['bootstrap']}",
            params={"install_id": self.install_id, "app_version": self.app_version},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("[signed_session:%s] bootstrap response: %s", self.namespace, data)
        if data.get("session_id") and data.get("session_secret") and data.get("expires_at"):
            self.session_id = data["session_id"]
            self.session_secret = data["session_secret"]
            self.expires_at = data["expires_at"]
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
            resp = await self._client.get(page_url, timeout=10, follow_redirects=True)
        except Exception as exc:
            logger.debug("[signed_session:%s] sitekey scrape fetch failed: %s", self.namespace, exc)
            return None
        if not resp.is_success:
            return None
        html = resp.text
        for pattern in (
            r'data-sitekey=["\']([0-9A-Za-z_-]{10,})["\']',
            r'[\'"]sitekey[\'"]\s*:\s*[\'"]([0-9A-Za-z_-]{10,})[\'"]',
            r'sitekey=([0-9A-Za-z_-]{10,})',
        ):
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None

    def _build_challenge_url(self, challenge_id: str) -> str:
        # Nessun parametro "cb" necessario: il grant si ottiene chiamando
        # {endpoints.challenge}/verify direttamente (confermato via
        # DevTools), non tramite notifica in background alla pagina.
        parts = list(urlparse(f"{self.base_url}{self.endpoints['challenge']}"))
        query = dict(parse_qsl(parts[4]))
        query["id"] = challenge_id
        parts[4] = urlencode(query)
        return urlunparse(parts)

    async def verify_challenge(self, challenge_id: str, turnstile_token: str) -> str:
        """
        Exchanges a solved Turnstile token for a grant by calling the
        challenge's own /verify endpoint directly - confirmed via DevTools
        to be POST {base_url}{endpoints.challenge}/verify with
        {"challenge_id": ..., "turnstile_token": ...}, returning
        {"grant": "...", "expires_in": ...}.

        This replaces the earlier (incorrect) assumption that the grant
        arrives via a background notification to a callback URL - the
        challenge page never notifies anyone, it just calls this endpoint
        itself and uses the grant locally. We can call it ourselves right
        after obtaining the token, with no callback/redirect needed at all.

        NOTE (fix): confrontando con DevTools la richiesta del browser vera
        e propria, l'API richiede anche un set di header "da browser"
        (Origin, User-Agent, Sec-Fetch-*, Sec-Ch-Ua, ecc.) — senza di essi
        risponde con un errore generico ("Invalid request"), anche con un
        token Turnstile valido. Questi header sono ora impostati come
        default su self._client in __init__ (_BROWSER_FINGERPRINT_HEADERS +
        Origin), quindi questa POST li eredita automaticamente. Se hai
        anche il cookie cf_clearance della sessione che ha risolto il
        Turnstile, chiama client.set_cf_clearance(...) PRIMA di questa
        funzione: è l'unico elemento visto nello screenshot che non può
        essere impostato staticamente (è legato alla sessione/fingerprint
        con cui è stato ottenuto).
        """
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
        self.pending_sitekey = None
        self.pending_challenge_id = None
        self._save()

    async def _refresh(self) -> None:
        refresh_path = self.endpoints.get("refresh")
        if not refresh_path:
            return  # nessun endpoint di refresh dichiarato: comportamento identico a Go
        body = {"install_id": self.install_id}
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
                if client.pending_sitekey and client.pending_challenge_id:
                    try:
                        from .turnstile import solve

                        # Il browser serve solo a ottenere il token
                        # Turnstile. Il grant si ottiene chiamando
                        # direttamente /challenge/verify (confermato via
                        # DevTools) - non c'è nessun canale di notifica in
                        # background da intercettare, quindi niente
                        # server locale, niente cb URL, niente attesa: si
                        # chiama verify() noi stessi subito dopo il token.
                        token = await asyncio.to_thread(
                            solve,
                            sitekey=client.pending_sitekey,
                            siteurl=client.pending_auth_url,
                            timeout=int(turnstile_timeout),
                        )
                        if token:
                            grant = await client.verify_challenge(
                                client.pending_challenge_id, token
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

        if resp.status_code in (401, 428):
            # Stesso comportamento del backend Go: una 401/428 invalida la
            # sessione locale e riparte la verifica invece di restituire
            # l'errore grezzo al chiamante.
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
        endpoints=manifest_block.get("endpoints"),
        data_dir=data_dir,
    )
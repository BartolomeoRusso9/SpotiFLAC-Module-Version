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

NOTE (2026-07-12, cattura di rete reale via HTTP Toolkit su SpotiFLAC-Mobile):
oltre a bootstrap/challenge/verify/exchange, esiste un flusso di DOWNLOAD a
"ticket" a valle della sessione firmata, non ancora implementato qui:

    POST {base_url}/tickets   (signed, come ogni altra signedFetch)
      body: {"capability": "download_ticket", "provider": "<short_id>",
             "resource_hash": "<sha256 esadecimale>"}
      → {"ticket_id": "tkt_...", "expires_in": 60, "max_uses": 1}

    POST {base_url}/dl/{short_id}   (signed, PIÙ un header extra)
      header extra: X-Zarz-Ticket: <ticket_id ottenuto sopra>
      body: {"id": "<track id del provider>", "quality": "<stringa qualità>"}
      → presumibilmente URL/stream del file (non catturato: la richiesta
        osservata ha ricevuto 502 dal server, quindi la risposta reale non è
        nota)

Il "resource_hash" è calcolato lato JS dall'estensione (probabilmente hash di
provider+id+qualità o simile) e non è replicabile qui senza il codice
JS dell'estensione specifica (es. tidal-web/index.js) che lo genera. Il
ticket è a singolo uso (max_uses=1) e vive solo 60s: va richiesto appena
prima di ogni singola chiamata /dl/{short_id}, non riutilizzato.
Se in futuro serve implementare anche questo, il metodo generico `request()`
già presente qui sotto (usato per signedFetch) può essere riusato invariato
per la POST /tickets — serve solo aggiungere il supporto per l'header extra
X-Zarz-Ticket sulla POST /dl/{short_id} (extra_headers è già supportato da
request()).
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
from typing import Any, Callable
from urllib.parse import parse_qsl, parse_qs, urlencode, urlparse, urlunparse

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

_LOCAL_CALLBACK_HOST = "127.0.0.1"
_LOCAL_CALLBACK_PATH = "/callback"
_MANUAL_GRANT_TIMEOUT_S = 300  # 5 minuti per incollare il grant


class _LocalGrantListener:
    """
    STORICO / NON PIÙ USATA dal flusso principale (authenticate_with_manual_grant).

    Ipotesi originaria: la pagina di sfida, dopo aver risolto il Turnstile,
    avrebbe fatto un redirect al "cb" fornito (come fa l'app Flutter con lo
    scheme "spotiflac://..."), catturabile da un piccolo server HTTP locale
    che sostituisse quello scheme mobile con http://127.0.0.1:{porta}.

    Verificato via DevTools che questo NON accade in un browser esterno: la
    pagina ottiene il grant chiamando internamente {endpoints.challenge}/verify,
    ma poi non naviga da nessuna parte (il meccanismo "cb" sembra pensato per
    una WebView nativa con bridge JS, non per un browser esterno). Il flusso
    attuale (authenticate_with_manual_grant) evita del tutto il problema:
    l'utente apre la pagina a mano, risolve il Turnstile, e legge/incolla
    lui stesso il grant dalla risposta di quella chiamata in DevTools. Questa
    classe resta solo per riferimento storico.
    """

    def __init__(self, host: str = _LOCAL_CALLBACK_HOST, path: str = _LOCAL_CALLBACK_PATH) -> None:
        self.host = host
        self.path = path
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._grant_future: asyncio.Future[str] | None = None

    async def start(self) -> str:
        """Avvia il listener su una porta libera e ritorna l'URL di callback completo."""
        loop = asyncio.get_running_loop()
        self._grant_future = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle_connection, host=self.host, port=0
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return f"http://{self.host}:{self.port}{self.path}"

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            # Consuma il resto degli header della richiesta (non ci servono)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if not line or line in (b"\r\n", b"\n"):
                    break

            try:
                method, raw_target, _ = request_line.decode("latin-1").split(" ", 2)
            except ValueError:
                method, raw_target = "GET", "/"

            parsed = urlparse(raw_target)

            # Ignora qualsiasi richiesta che non sia il nostro path di callback
            # (es. GET /favicon.ico che alcuni browser sparano automaticamente):
            # non deve MAI toccare il future del grant.
            if parsed.path != self.path:
                await self._respond(writer, 404, "<h3>Not Found</h3>")
                return

            params = parse_qs(parsed.query)
            grant = (params.get("grant") or [None])[0]
            error = (params.get("error") or [None])[0]

            if grant:
                await self._respond(
                    writer, 200,
                    "<h2>Verifica completata ✅</h2>"
                    "<p>Puoi chiudere questa finestra e tornare all'app.</p>",
                )
            else:
                await self._respond(
                    writer, 200,
                    "<h2>Verifica non riuscita</h2>"
                    f"<p>{error or 'Nessun grant ricevuto.'}</p>",
                )

            if self._grant_future is not None and not self._grant_future.done():
                if grant:
                    self._grant_future.set_result(grant)
                else:
                    self._grant_future.set_exception(
                        RuntimeError(f"callback senza grant: {error or 'sconosciuto'}")
                    )
        except Exception as exc:
            if self._grant_future is not None and not self._grant_future.done():
                self._grant_future.set_exception(exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, html_body: str) -> None:
        reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
        body = (
            f"<html><body style='font-family:sans-serif;text-align:center;"
            f"padding-top:4em'>{html_body}</body></html>"
        ).encode("utf-8")
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        writer.write(response)
        await writer.drain()

    async def wait_for_grant(self, timeout: float) -> str:
        """Blocca finché il browser non chiama il callback con ?grant=..., poi ferma il listener."""
        assert self._grant_future is not None, "start() non è stato chiamato"
        try:
            return await asyncio.wait_for(self._grant_future, timeout=timeout)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None


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
        # Campi opzionali restituiti da bootstrap/exchange/refresh — confermati
        # via cattura di rete reale (2026-07-12): la risposta di
        # POST .../session/exchange include anche "refresh_after" (timestamp
        # assoluto, preferito rispetto al nostro skew calcolato) e
        # "capabilities" (lista di permessi della sessione, es.
        # ["resolve", "metadata", "download_ticket"]).
        self.refresh_after = record.get("refresh_after")
        self.capabilities = record.get("capabilities", [])
        self._save()

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
        # STORICO/DEPRECATO: questo helper NON aggiunge nessun "cb" — era
        # basato sull'assunzione (rivelatasi sbagliata, vedi verify_challenge
        # sotto) che il grant si ottenesse chiamando {endpoints.challenge}/verify
        # direttamente. Il vero backend (extension_signed_session.go,
        # buildSignedSessionChallengeURL) aggiunge SEMPRE un parametro "cb"
        # con l'URL di callback. Per il flusso corretto vedi
        # _build_challenge_url_with_callback() + authenticate_with_browser().
        # Questo metodo resta solo per lo scraping best-effort del sitekey
        # dentro bootstrap() (vedi _scrape_sitekey_from_page), che oggi non
        # serve più (non automatizziamo più la risoluzione del Turnstile),
        # ma è innocuo lasciarlo.
        parts = list(urlparse(f"{self.base_url}{self.endpoints['challenge']}"))
        query = dict(parse_qsl(parts[4]))
        query["id"] = challenge_id
        parts[4] = urlencode(query)
        return urlunparse(parts)

    def _build_challenge_url_with_callback(self, challenge_id: str, callback_url: str) -> str:
        """
        Replica ESATTAMENTE buildSignedSessionChallengeURL() del backend Go
        (extension_signed_session.go):

          1. il callback riceve, nella propria query string, cb_version=v2grant
             e state=<namespace> (nel Go è state=<extensionID>: qui usiamo il
             namespace del client, dato che un'istanza Python serve un solo
             "extension logico" alla volta);
          2. l'URL della pagina di sfida ({base}/challenge) riceve
             id=<challenge_id> e cb=<callback_url completo, urlencoded>.

        `callback_url` qui è tipicamente quello restituito da
        _LocalGrantListener.start(), cioè http://127.0.0.1:{porta}/callback
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

        Uso:
          1. bootstrap() ottiene un challenge_id.
          2. L'URL della pagina di sfida viene mostrato (via
             on_verification_url, o stampato/loggato di default).
          3. TU apri quell'URL in un browser qualsiasi e risolvi il Turnstile.
          4. Apri DevTools → tab Network → cerca la richiesta "verify" (POST
             a .../challenge/verify) → tab Preview: lì trovi
             {"grant": "gr_...", "expires_in": 60}.
          5. Copi quel valore di "grant" (senza virgolette) e lo incolli
             quando richiesto (o lo passi tramite `grant_input`).
          6. exchange_grant(grant) scambia il grant per una sessione vera.

        Il grant ha vita breve (~60s dalla risposta di verify): copialo e
        incollalo il più velocemente possibile.

        Parametri:
          on_verification_url – callback per mostrare l'URL (altrimenti
              stampato su stdout e loggato a WARNING).
          grant_input – funzione senza argomenti che ritorna il grant come
              stringa (utile per integrazioni non interattive/GUI). Se non
              fornita, chiede il grant via input() da terminale.
          timeout – secondi massimi di attesa per l'inserimento del grant
              (default 5 minuti). Solleva RuntimeError se scade prima che
              tu (o grant_input) fornisca un valore. NOTA: se l'attesa è su
              input() da terminale, il thread bloccato su quella chiamata
              non viene interrotto allo scadere del timeout (limite di
              Python: non si può cancellare un input() bloccante) — resta
              in attesa in background finché non premi Invio, ma la
              funzione ritorna comunque con l'errore di timeout appena
              scattato, senza aspettarlo.
        """
        boot_result = await self.bootstrap()
        if boot_result is True:
            return  # sessione ottenuta direttamente, nessuna verifica necessaria

        if not self.pending_challenge_id:
            if boot_result:
                self._emit_verification_url(boot_result, on_verification_url)
            raise RuntimeError(
                "Il server ha fornito un auth_url senza challenge_id: "
                "impossibile costruire l'URL della sfida."
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
                "Preview → campo 'grant'): ",
            )

        try:
            grant = await asyncio.wait_for(grant_awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Timeout ({timeout}s) in attesa dell'inserimento del grant"
            ) from exc

        grant = grant.strip()
        if not grant:
            raise RuntimeError("Nessun grant fornito.")

        await self.exchange_grant(grant)

    @staticmethod
    def _emit_verification_url(
        url: str, callback: "Callable[[str], None] | None"
    ) -> None:
        """
        Rende disponibile l'URL di verifica al chiamante, senza mai aprirlo
        automaticamente in un browser.

        - Se `callback` è fornito, gli viene passato l'URL (il chiamante
          decide cosa farne: webbrowser.open(), UI, notifica, ecc.).
        - Altrimenti viene stampato su stdout e loggato a livello WARNING,
          così resta visibile anche con la configurazione di logging di
          default (WARNING) usata da SpotiFLAC(...).
        """
        if callback is not None:
            callback(url)
            return
        logger.warning("[signed_session] Verifica richiesta: %s", url)
        print(f"\n[SpotiFLAC] Verifica richiesta — apri questo link nel browser:\n  {url}\n")

    async def verify_challenge(self, challenge_id: str, turnstile_token: str) -> str:
        """
        NON CHIAMARLO DIRETTAMENTE da Python — lasciato solo per riferimento.

        Questo endpoint ESISTE davvero ed è esattamente questo: POST
        {base_url}{endpoints.challenge}/verify con
        {"challenge_id": ..., "turnstile_token": ...}, risposta
        {"grant": "...", "expires_in": 60} — confermato via DevTools su una
        chiamata reale della pagina di sfida (200 OK).

        Il motivo per cui chiamarlo noi stessi da Python fallisce (400): quella
        richiesta, quando la fa la pagina, include un cookie `cf_clearance`
        di Cloudflare legato a quella specifica tab/sessione browser (oltre
        agli header di fingerprint già impostati su self._client). Un client
        HTTP headless come questo non può ottenere quel cookie: richiede di
        aver risolto realmente la sfida Cloudflare in un browser vero.

        Il flusso corretto (vedi authenticate_with_manual_grant) non replica
        questa chiamata: lasci che sia la PAGINA STESSA a farla (nel browser,
        con i suoi cookie), e ne leggi/incolli tu il risultato dalla tab
        Network di DevTools.
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
        self.refresh_after = data.get("refresh_after")
        self.capabilities = data.get("capabilities", [])
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
            self.refresh_after = data.get("refresh_after", self.refresh_after)
            self.capabilities = data.get("capabilities", self.capabilities)
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

            # Preferisci "refresh_after" (timestamp assoluto dato dal server,
            # confermato via cattura di rete reale: es. 2h prima di expires_at,
            # non 1h come il nostro skew di default) — più preciso dello skew
            # calcolato, che resta solo un fallback se il server non lo manda.
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

    # ─────────────────────── ticket / download layer ──────────────────────
    #
    # Formula e struttura confermate leggendo il codice sorgente reale
    # dell'estensione tidal-web (index.js, funzione signedTicket) e
    # verificate contro una cattura di rete reale (2026-07-12):
    #   sha256("tid:track:530979474") == "a5f4aee7d242692d616b4210cd61c48933b..."
    # che è esattamente il resource_hash osservato nella richiesta reale.
    # Questa parte è generica: vale per qualsiasi provider (non solo Tidal),
    # dato che è il runtime signedSession a gestirla, non il singolo
    # provider — cambia solo cosa il provider mette come body della POST
    # /dl/{provider} (quello sì è specifico per provider).

    @staticmethod
    def compute_resource_hash(provider: str, resource_id: str, resource_type: str = "track") -> str:
        """
        Calcola il resource_hash richiesto da POST /tickets, ESATTAMENTE come
        fa il JS delle estensioni ufficiali (es. tidal-web/index.js,
        signedTicket()):

            sha256(f"{provider}:{resource_type}:{str(resource_id).lower()}")

        Es. compute_resource_hash("tid", "530979474") per una traccia Tidal
        (il "type" di default è "track", come nel JS: `type || "track"`).
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
        """
        Ottiene un ticket di download monouso (max_uses=1, vita breve ~60s,
        confermato via cattura di rete reale) per la risorsa indicata,
        chiamando POST {tickets_path} con body:
            {"capability": "download_ticket", "provider": provider,
             "resource_hash": compute_resource_hash(provider, resource_id, resource_type)}

        Il ticket va usato IMMEDIATAMENTE con ticketed_request() — non va
        richiesto in anticipo/cacheato, dato che è a singolo uso e scade in
        pochi secondi.
        """
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
        per una POST firmata a `dl_path`, aggiungendo l'header
        "X-Zarz-Ticket: <ticket_id>" come fa il JS (postDownloadAPI()):

            ticket_id = await get_download_ticket(provider, resource_id, resource_type)
            POST {dl_path} con json_body, header extra X-Zarz-Ticket=ticket_id

        La risposta e il body di `json_body` restano specifici del singolo
        provider (es. per Tidal: {"id": track_id, "quality": "LOSSLESS"},
        risposta {"data": {"manifest": ..., "audioQuality": ..., ...}} — un
        manifest DASH/XML da parsare ulteriormente in modo specifico per
        Tidal, non generico) — questo metodo si occupa solo del livello
        "ticket + header", non del parsing del risultato.
        """
        ticket_id = await self.get_download_ticket(
            provider, resource_id, resource_type, tickets_path=tickets_path
        )
        return await self.request(
            "POST", dl_path, json_body=json_body,
            extra_headers={"X-Zarz-Ticket": ticket_id},
        )

    async def aclose(self) -> None:
        await self._client.aclose()


async def perform_signed_fetch(
    client: SignedSessionClient,
    method: str,
    path: str,
    body: Any,
    headers: dict | None,
    on_verification_url: "Callable[[str], None] | None" = None,
    grant_input: "Callable[[], str] | None" = None,
    timeout: float = _MANUAL_GRANT_TIMEOUT_S,
) -> dict:
    """
    High-level handler for a `session.signedFetch(method, path, body, headers)`
    call coming from the JS extension worker. Transparently bootstraps the
    session the first time it's needed, then performs the signed request.

    L'autenticazione (quando serve) è completamente MANUALE, senza nessun
    browser automatizzato: l'URL di verifica viene passato a
    `on_verification_url(url)` se fornito (altrimenti stampato/loggato), tu
    lo apri in un browser qualsiasi, risolvi il Turnstile, e incolli il
    grant che vedi in DevTools → Network → verify → Preview
    ({"grant": "gr_...", "expires_in": 60}) — vedi
    SignedSessionClient.authenticate_with_manual_grant() per i dettagli.
    `timeout` limita quanto aspettare l'inserimento del grant (default 5 min).

    Returns a JSON-serializable dict matching what index.js's signedJSON()
    expects: {"statusCode": int, "body": str} on success, or {"error": str}
    on failure (inclusa una verifica non completata/annullata/scaduta).
    """
    try:
        if not client.authenticated:
            try:
                await client.authenticate_with_manual_grant(
                    on_verification_url=on_verification_url,
                    grant_input=grant_input,
                    timeout=timeout,
                )
            except Exception as exc:
                logger.warning(
                    "[signed_session:%s] Autenticazione manuale fallita: %s",
                    client.namespace, exc,
                )
                return {"error": str(exc)}

        resp = await client.request(method, path, json_body=body, extra_headers=headers)

        if resp.status_code in (401, 428):
            # Stesso comportamento del backend Go: una 401/428 invalida la
            # sessione locale e riparte la verifica invece di restituire
            # l'errore grezzo al chiamante. Qui NON riapriamo subito un
            # browser durante una richiesta "di passaggio": segnaliamo solo
            # che serve una nuova verifica, lasciando al chiamante decidere
            # quando invocare di nuovo authenticate_with_browser().
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
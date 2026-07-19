import os
import re
import json
import time
import hmac
import hashlib
import secrets
import base64
import threading
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import queue
import requests
import logging

from ..core.endpoints import get_community_url

logger = logging.getLogger(__name__)

# Costanti
COMMUNITY_SESSION_SKEW = timedelta(minutes=5)
COMMUNITY_VERIFY_TIMEOUT = 300  # secondi (5 minuti)


def is_docker() -> bool:
    """Rileva se il codice è in esecuzione dentro un container Docker."""
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


def fetch_latest_version() -> str:
    url = "https://api.github.com/repos/spotbye/SpotiFLAC/releases/latest"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()

        tag_name = response.json().get("tag_name", "")
        return tag_name.lstrip("v")

    except requests.RequestException as e:
        logger.warning("No version retrieved from GitHub: %s", e)
        return ""


APP_VERSION = fetch_latest_version()

community_session_mu = threading.Lock()
community_browser_mu = threading.Lock()
community_browser_open = None
community_window_foreground = None


@dataclass
class CommunitySessionRecord:
    install_id: str = ""
    session_id: str = ""
    session_secret: str = ""
    expires_at: str = ""


@dataclass
class CommunitySessionExchange:
    session_id: str = ""
    session_secret: str = ""
    expires_at: str = ""


def ensure_app_dir() -> str:
    """Restituisce la cartella dell'app."""
    app_dir = os.path.expanduser("~/.spotiflac")
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


def set_community_verification_handlers(open_browser_func, foreground_func):
    global community_browser_open, community_window_foreground
    with community_browser_mu:
        community_browser_open = open_browser_func
        community_window_foreground = foreground_func


def community_session_path() -> str:
    directory = ensure_app_dir()
    os.chmod(directory, 0o700)
    return os.path.join(directory, "community_session.json")


def load_community_session() -> CommunitySessionRecord:
    path = community_session_path()
    record = CommunitySessionRecord()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                record = CommunitySessionRecord(**data)
        except Exception:
            pass

    if not record.install_id.strip():
        record.install_id = community_random_hex(16)
        save_community_session(record)

    return record


def save_community_session(record: CommunitySessionRecord):
    path = community_session_path()
    data = json.dumps(asdict(record), indent=2)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(data)

    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)
    os.chmod(path, 0o600)


def community_session_valid(record: CommunitySessionRecord) -> bool:
    if not record or not record.session_id or not record.session_secret:
        return False
    try:
        # Gestisce il formato RFC3339Nano terminante con 'Z'
        expires_str = record.expires_at.replace("Z", "+00:00")
        expires_at = datetime.fromisoformat(expires_str)
        return (expires_at - datetime.now(timezone.utc)) > COMMUNITY_SESSION_SKEW
    except Exception:
        return False


def ensure_community_session() -> CommunitySessionRecord:
    with community_session_mu:
        record = load_community_session()

        if community_session_valid(record):
            return record

        grant = run_community_verification(record)
        exchanged = exchange_community_grant(record, grant)

        record.session_id = exchanged.session_id
        record.session_secret = exchanged.session_secret
        record.expires_at = exchanged.expires_at

        save_community_session(record)
        return record


def clear_community_session_credentials():
    with community_session_mu:
        try:
            record = load_community_session()
            record.session_id = ""
            record.session_secret = ""
            record.expires_at = ""
            save_community_session(record)
        except Exception:
            pass


def _run_manual_terminal_verification(challenge_url: str) -> str:
    """
    Fallback da terminale (o Docker/Telegram).
    Mostra l'URL e attende l'input dell'utente su sys.stdin.
    """
    print(
        f"\n[SpotiFLAC] Verification required — open this link in the browser:\n  {challenge_url}\n"
    )
    try:
        grant = input(
            "Incolla qui il grant (da DevTools → Network → verify → Preview → field 'grant'): "
        )
        grant = grant.strip()
        if not grant:
            raise RuntimeError("No grant provided.")
        return grant
    except EOFError:
        raise Exception("verification cancelled (EOF)")


def run_community_verification(record: CommunitySessionRecord) -> str:
    grant_queue = queue.Queue(maxsize=1)
    callback_state = community_random_hex(16)

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed_path = urllib.parse.urlparse(self.path)
            if parsed_path.path != "/session-grant":
                self.send_error(404)
                return

            # Errore di battitura corretto qui: parse_qs
            qs = urllib.parse.parse_qs(parsed_path.query)
            state = qs.get("state", [""])[0]

            if not hmac.compare_digest(state.encode(), callback_state.encode()):
                self.send_error(400, "Invalid verification callback state")
                return

            grant = qs.get("grant", [""])[0].strip()
            if not grant:
                self.send_error(400, "Missing verification grant")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            html = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Verified</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:#000;background-image:radial-gradient(circle,rgba(255,255,255,.2) 1.5px,transparent 1.5px);background-size:30px 30px;color:#f5f5f5;font:14px/1.5 Inter,sans-serif}main{text-align:center}.icon{width:48px;height:48px;margin:0 auto 20px;display:grid;place-items:center;border-radius:50%;background:#fff;color:#000;font-size:22px}h1{margin:0 0 6px;font-size:24px;letter-spacing:-.035em}p{margin:0;color:#888}</style></head><body><main><div class="icon">&#10003;</div><h1>Verified</h1><p>Returning to SpotiFLAC...</p></main><script>setTimeout(()=>window.close(),700)</script></body></html>'
            self.wfile.write(html.encode("utf-8"))

            try:
                grant_queue.put_nowait(grant)
            except queue.Full:
                pass

            with community_browser_mu:
                foreground = community_window_foreground
            if foreground:
                foreground()

        def log_message(self, format, *args):
            pass  # Disabilita i log standard del server HTTP

    # Avvia il server su una porta casuale libera
    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    callback_url = f"http://127.0.0.1:{port}/session-grant?state={callback_state}"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        verify_base_url = get_community_url("verify")
        if not verify_base_url:
            raise Exception("verification endpoint is unavailable")

        # 1. Bootstrap
        bootstrap_url = f"{verify_base_url}/bootstrap"
        params = {
            "install_id": record.install_id,
            "app_version": community_app_version(),
            "platform": "desktop",
        }

        resp = requests.get(bootstrap_url, params=params, timeout=15)
        if resp.status_code != 200:
            raise Exception(f"verification bootstrap returned HTTP {resp.status_code}")

        result = resp.json()
        challenge_url_str = result.get("challenge_url")

        if not challenge_url_str or not challenge_url_str.startswith("https://"):
            raise Exception("verification service returned an invalid challenge URL")

        # Aggiungiamo il callback URL al challenge URL
        parsed_challenge = urllib.parse.urlparse(challenge_url_str)
        challenge_qs = urllib.parse.parse_qs(parsed_challenge.query)
        challenge_qs["cb"] = [callback_url]

        new_query = urllib.parse.urlencode(challenge_qs, doseq=True)
        final_challenge_url = urllib.parse.urlunparse(
            parsed_challenge._replace(query=new_query)
        )

        # === MODO 1: GUI Integrata (Se configurata tramite la UI di SpotiFLAC) ===
        with community_browser_mu:
            open_browser = community_browser_open

        if open_browser:
            open_browser(final_challenge_url)
            try:
                grant = grant_queue.get(timeout=COMMUNITY_VERIFY_TIMEOUT)
                return grant
            except queue.Empty:
                raise Exception("verification timed out (GUI browser)")

        # === MODO 2: Automazione via solver.py (Playwright/Selenium) ===
        if not is_docker():
            logger.info("Attempting automated verification via solver.py...")
            try:
                from ..core.solver import solve_with_callback

                # Prova ad estrarre la sitekey se esposta nella pagina HTML
                sitekey = ""
                try:
                    html_resp = requests.get(final_challenge_url, timeout=10)
                    for pattern in (
                        r'data-sitekey=["\']([0-9A-Za-z_-]{10,})["\']',
                        r"sitekey=([0-9A-Za-z_-]{10,})",
                    ):
                        match = re.search(pattern, html_resp.text)
                        if match:
                            sitekey = match.group(1)
                            break
                except Exception:
                    pass

                # Invoca il solver (sincrono)
                token, grant = solve_with_callback(
                    sitekey, final_challenge_url, 60, 3.0
                )
                if grant:
                    logger.info("Automated verification successful!")
                    return grant
                else:
                    logger.warning(
                        "Solver finished but no grant was found in network traffic."
                    )

            except ImportError:
                logger.info("solver.py not found or Playwright dependencies missing.")
            except Exception as e:
                logger.warning(f"Automated verification failed: {e}")

        # === MODO 3: Fallback Manuale via Terminale (Es. Bot Telegram / Docker) ===
        logger.info("Falling back to manual terminal input.")
        return _run_manual_terminal_verification(final_challenge_url)

    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)


def exchange_community_grant(
    record: CommunitySessionRecord, grant: str
) -> CommunitySessionExchange:
    payload = {
        "grant": grant,
        "install_id": record.install_id,
        "app_version": community_app_version(),
        "platform": "desktop",
    }

    verify_base_url = get_community_url("verify")
    if not verify_base_url:
        raise Exception("verification endpoint is unavailable")

    url = f"{verify_base_url}/session/exchange"
    resp = requests.post(url, json=payload, timeout=15)

    if resp.status_code != 200:
        raise Exception(f"session exchange returned HTTP {resp.status_code}")

    data = resp.json()
    if (
        not data.get("session_id")
        or not data.get("session_secret")
        or not data.get("expires_at")
    ):
        raise Exception("session exchange response is incomplete")

    return CommunitySessionExchange(**data)


def sign_community_request(
    method: str, url: str, body: bytes, record: CommunitySessionRecord
) -> dict:
    """
    Ritorna un dizionario di header da aggiungere alla richiesta.
    (Non modifica un oggetto http.Request in-place come in Go, ma restituisce gli header).
    """
    body_hash = hashlib.sha256(body if body else b"").hexdigest()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    nonce = community_random_hex(12)

    parsed_timestamp = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.000Z").replace(
        tzinfo=timezone.utc
    )
    window = int(parsed_timestamp.timestamp()) // 300

    rolling_input = f"{window}:{record.session_id}".encode("utf-8")
    rolling_key = community_hmac(record.session_secret.encode("utf-8"), rolling_input)

    parsed_url = urllib.parse.urlparse(url)
    escaped_path = urllib.parse.quote(parsed_url.path)

    signing_parts = [
        "SPOTIFLAC-HMAC-V1",
        method.upper(),
        escaped_path,
        "",
        body_hash,
        timestamp,
        nonce,
        record.session_id,
        community_app_version(),
        "desktop",
    ]
    signing_input = "\n".join(signing_parts).encode("utf-8")

    signature_bytes = community_hmac(rolling_key, signing_input)
    # Codifica Base64 Raw URLEncoding (senza padding '=')
    signature = base64.urlsafe_b64encode(signature_bytes).decode("utf-8").rstrip("=")

    return {
        "X-Sig-Session": record.session_id,
        "X-Sig-Timestamp": timestamp,
        "X-Sig-Nonce": nonce,
        "X-Sig-Body-SHA256": body_hash,
        "X-Sig-Signature": signature,
        "X-Sig-App-Version": community_app_version(),
        "X-Sig-Platform": "desktop",
    }


# --- Utility Cryptografiche & Varie ---


def community_app_version() -> str:
    version = APP_VERSION.strip()
    if not version or version == "Unknown":
        return "unknown"
    return version


def community_random_hex(size: int) -> str:
    try:
        return secrets.token_hex(size)
    except Exception:
        # Fallback come nel codice Go in caso rand fallisca (raro in Python)
        return str(time.time_ns())


def community_hmac(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()

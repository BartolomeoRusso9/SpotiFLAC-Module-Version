import json
import hashlib
import base64
import os
import httpx
import logging
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_SEED_PARTS = [b"spotif", b"lac:co", b"mmunity:url:v1"]
_AAD = b"spotiflac|community|url|v1"

_CLOUD_URL = "https://gist.githubusercontent.com/BartolomeoRusso9/0b857131a77131be2c7b2b0131c3f2cf/raw/gistfile1.txt"

_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".endpoints_cache.txt")


def _decrypt_base64_payload(b64_string: str) -> dict:
    """Decripta la stringa unificata di GitHub."""
    raw_bytes = base64.b64decode(b64_string.strip())
    
    # Separiamo i pezzi come li avevamo uniti
    nonce = raw_bytes[:12]
    encrypted_payload = raw_bytes[12:]
    
    hasher = hashlib.sha256()
    for part in _SEED_PARTS:
        hasher.update(part)
    key = hasher.digest()
    
    aesgcm = AESGCM(key)
    decrypted_bytes = aesgcm.decrypt(nonce, encrypted_payload, _AAD)
    
    return json.loads(decrypted_bytes.decode('utf-8'))

def _load_registry() -> dict:
    """Scarica il JSON crittografato da GitHub, o usa il backup locale."""
    try:
        req = httpx.get(_CLOUD_URL, headers={'User-Agent': 'SpotiFLAC-Agent'}, timeout=3.0)
        req.raise_for_status()
        cloud_string = req.text

        registry = _decrypt_base64_payload(cloud_string)
        
        try:
            with open(_CACHE_FILE, "w") as f:
                f.write(cloud_string)
        except Exception:
            pass
            
        return registry

    except Exception as e:
        logger.warning(f"Unable to contact Cloud servers ({e}). Falling back to local cache...")
        
        # 2. Se fallisce, prova a leggere l'ultima cache salvata
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, "r") as f:
                    cached_string = f.read()
                return _decrypt_base64_payload(cached_string)
        except Exception as cache_e:
            logger.error(f"Unable to read local cache: {cache_e}")
            
        return {}

# Questa riga viene executeta solo la prima volta che il file viene importato
REGISTRY = _load_registry()


# ─── 5. FUNZIONI HELPER PER I PROVIDER (Personali) ──────────────────────

def get_qobuz_endpoints(category: str) -> list[str]:
    return REGISTRY.get("qobuz", {}).get(category, [])

def get_tidal_post_endpoints() -> list[str]:
    return REGISTRY.get("tidal", {}).get("post", [])

def get_deezer_endpoint(key: str) -> str:
    """Chiavi valide: 'resolver', 'flacdownloader_prepare', 'flacdownloader_asset'"""
    return REGISTRY.get("deezer", {}).get(key, "")

def get_amazon_endpoint(key: str) -> str:
    """Chiavi: 'musicdl', 'spotbye1', 'spotbye2', 'zarz', 'squid'"""
    return REGISTRY.get("amazon", {}).get(key, "")

def get_apple_music_endpoint(key: str) -> str:
    """Chiavi: 'proxy_direct', 'proxy_queued'"""
    return REGISTRY.get("apple_music", {}).get(key, "")

def get_asian_provider_endpoint(provider: str, key: str) -> str:
    """Per joox, kuwo, migu, netease"""
    return REGISTRY.get(provider, {}).get(key, "")

def get_soundcloud_cobalt() -> str:
    return REGISTRY.get("soundcloud", {}).get("cobalt", "")

def get_youtube_endpoints(key: str) -> list[str] | str:
    """Chiavi: 'cobalt', 'zarz_clean', 'zarz_dl'"""
    return REGISTRY.get("youtube", {}).get(key, [])

def get_pandora_base_and_path() -> tuple[str, str]:
    pan = REGISTRY.get("pandora", {})
    return pan.get("zarz_base", ""), pan.get("zarz_dl", "")

def get_health_zarz_url() -> str:
    return REGISTRY.get("health", {}).get("zarz", "")

def get_community_url(provider: str) -> str:
    """Returns l'URL Community se esiste nel registro, altrimenti stringa vuota."""
    return REGISTRY.get(provider, {}).get("community", "")

def _jwt_payload(token: str) -> dict:
    """
    Decodifica (senza verificare la firma) il payload di un JWT
    'Bearer <header>.<payload>.<signature>'. Usata solo per leggere
    campi informativi come 'exp', non per validare l'autenticità.
    """
    try:
        raw = token.removeprefix("Bearer ").strip()
        parts = raw.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # JWT usa base64url senza padding: va ripristinato prima di decodificare.
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return {}

def get_monochrome_token() -> str:
    """
    Ritorna il token (con prefisso 'Bearer ') letto dal registro cifrato.
    Se il token è un JWT con campo 'exp', verifica la scadenza e logga un
    warning se è già scaduto o scade entro 24 ore — non viene effettuato
    alcun refresh automatico: il rinnovo resta manuale (rigenerare e
    pubblicare un nuovo Gist cifrato).
    """
    import time

    token = REGISTRY.get("monochrome-token", {}).get("token", "")
    if not token:
        return token

    payload = _jwt_payload(token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        now = time.time()
        if exp <= now:
            expired_since = now - exp
            logger.debug(
                "Token già scaduto da %.0f ore — "
                "il proxy Tidal probabilmente risponderà 401. "
                "Rigenerare il token e aggiornare il Gist.",
                expired_since / 3600,
            )
        elif exp - now <= 86400:
            logger.debug(
                "Token in scadenza entro 24 ore (alle %s UTC).",
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(exp)),
            )

    return token

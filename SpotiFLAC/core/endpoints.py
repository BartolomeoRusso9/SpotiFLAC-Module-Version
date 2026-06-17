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

_CLOUD_URL = "https://gist.githubusercontent.com/BartolomeoRusso9/0b857131a77131be2c7b2b0131c3f2cf/raw/28f149e3e90a6a3783e72e91be7f84b8c811be45/gistfile1.txt"

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
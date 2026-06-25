import json
import hashlib
import base64
import os
import time
from ..core.http import httpx
import logging
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_SEED_PARTS = [b"spotif", b"lac:co", b"mmunity:url:v1"]
_AAD = b"spotiflac|community|url|v1"

_CLOUD_URL = "https://gist.githubusercontent.com/BartolomeoRusso9/ef9fdbbc894818aea89d25a8d99f8c77/raw/"

_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".endpoints_cache.txt")


def _decrypt_base64_payload(b64_string: str) -> dict:
    """Decrypt the unified string from GitHub."""
    
    # 1. Remove all spaces, newlines and carriage returns (not just at the edges)
    clean_b64 = "".join(b64_string.split())
    
    # 2. Convert Base64 URL-Safe to Standard
    clean_b64 = clean_b64.replace('-', '+').replace('_', '/')
    
    # 3. FUNDAMENTAL FIX: Force to pure ASCII format, sweeping away invisible characters like BOM
    clean_b64 = clean_b64.encode('ascii', 'ignore').decode('ascii')
    
    # 4. Padding safety
    padding_needed = len(clean_b64) % 4
    if padding_needed:
        clean_b64 += '=' * (4 - padding_needed)

    # Now the string is perfectly clean and ready for decoding
    raw_bytes = base64.b64decode(clean_b64)
    
    # Separate the pieces as we had joined them
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
    """Download the encrypted JSON from GitHub, or use the local backup."""
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
        
        # 2. If it fails, try to read the last saved cache
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, "r") as f:
                    cached_string = f.read()
                return _decrypt_base64_payload(cached_string)
        except Exception as cache_e:
            logger.error(f"Unable to read local cache: {cache_e}")
            
        return {}

# In-memory cache with TTL: the Gist is rechecked after _TTL_SECONDS seconds.
# Increase the value to reduce network calls in long-running processes.
_TTL_SECONDS: int = 30
_registry_cache: dict = {}
_registry_fetched_at: float = 0.0

def _get_registry() -> dict:
    """Return the registry, reloading from the Gist if the TTL has expired."""
    global _registry_cache, _registry_fetched_at
    if not _registry_cache or (time.time() - _registry_fetched_at) >= _TTL_SECONDS:
        _registry_cache = _load_registry()
        _registry_fetched_at = time.time()
    return _registry_cache


# ─── PROVIDER HELPER FUNCTIONS ──────────────────────

def get_qobuz_endpoints(category: str) -> list[str]:
    return _get_registry().get("qobuz", {}).get(category, [])

def get_tidal_post_endpoints() -> list[str]:
    return _get_registry().get("tidal", {}).get("post", [])

def get_deezer_endpoint(key: str) -> str:
    """Valid keys: 'resolver', 'flacdownloader_prepare', 'flacdownloader_asset'"""
    return _get_registry().get("deezer", {}).get(key, "")

def get_amazon_endpoint(key: str) -> str:
    """
    Valid keys:
    - Download: 'musicdl', 'spotbye1', 'spotbye2', 'zarz', 'zarz_media', 'community'
    - S: 's', 's_home', 's_challenge', 's_verify', 's_stream', 's_queue'
    - Resolver: 'resolver_songstats', 'resolver_songlink_api', 'resolver_songlink_html', 'resolver_spotify', 'resolver_deezer'
    - Base: 'amazon_music_base'
    """
    return _get_registry().get("amazon", {}).get(key, "")

def get_apple_music_endpoint(key: str) -> str:
    """Keys: 'proxy_direct', 'proxy_queued'"""
    return _get_registry().get("apple_music", {}).get(key, "")

def get_asian_provider_endpoint(provider: str, key: str) -> str:
    """For joox, kuwo, migu, netease"""
    return _get_registry().get(provider, {}).get(key, "")

def get_soundcloud_cobalt() -> str:
    return _get_registry().get("soundcloud", {}).get("cobalt", "")

def get_youtube_endpoints(key: str) -> list[str] | str:
    """Keys: 'cobalt', 'zarz_clean', 'zarz_dl'"""
    return _get_registry().get("youtube", {}).get(key, [])

def get_pandora_base_and_path() -> tuple[str, str]:
    pan = _get_registry().get("pandora", {})
    return pan.get("zarz_base", ""), pan.get("zarz_dl", "")

def get_health_zarz_url() -> str:
    return _get_registry().get("health", {}).get("zarz", "")

def get_community_url(provider: str) -> str:
    """Return the Community URL if it exists in the registry, otherwise empty string."""
    return _get_registry().get(provider, {}).get("community", "")

def _jwt_payload(token: str) -> dict:
    """
    Decode (without verifying the signature) the payload of a JWT
    'Bearer <header>.<payload>.<signature>'. Used only to read
    informational fields like 'exp', not to validate authenticity.
    """
    try:
        raw = token.removeprefix("Bearer ").strip()
        parts = raw.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # JWT uses base64url without padding: must be restored before decoding.
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return {}

def get_monochrome_token() -> str:
    """
    Return the token (with 'Bearer ' prefix) read from the encrypted registry.
    If the token is a JWT with 'exp' field, check expiration and log a
    warning if already expired or expires within 24 hours — no automatic
    refresh is performed: renewal remains manual (regenerate and
    publish a new encrypted Gist).
    """
    import time

    token = _get_registry().get("monochrome-token", {}).get("token", "")
    if not token:
        return token

    payload = _jwt_payload(token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        now = time.time()
        if exp <= now:
            expired_since = now - exp
            logger.debug(
                "Token already expired since %.0f hours ago — "
                "the Tidal proxy will likely respond with 401. "
                "Regenerate the token and update the Gist.",
                expired_since / 3600,
            )
        elif exp - now <= 86400:
            logger.debug(
                "Token expires in 24 hours (at %s UTC).",
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(exp)),
            )

    return token

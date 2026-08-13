from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from SpotiFLAC.core.http import httpx

logger = logging.getLogger(__name__)

_SEED_PARTS = [b"spotif", b"lac:co", b"mmunity:url:v1"]
_AAD = b"spotiflac|community|url|v1"

_CLOUD_URL = "https://gist.githubusercontent.com/BartolomeoRusso9/ef9fdbbc894818aea89d25a8d99f8c77/raw"

_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".endpoints_cache.txt")


def _decrypt_base64_payload(b64_string: str) -> dict:
    """Decrypt the unified string from GitHub."""
    # 1. Remove all spaces, newlines and carriage returns (not just at the edges)
    clean_b64 = "".join(b64_string.split())

    # 2. Convert Base64 URL-Safe to Standard
    clean_b64 = clean_b64.replace("-", "+").replace("_", "/")

    # 3. FUNDAMENTAL FIX: Force to pure ASCII format, sweeping away invisible characters like BOM
    clean_b64 = clean_b64.encode("ascii", "ignore").decode("ascii")

    # 4. Padding safety
    padding_needed = len(clean_b64) % 4
    if padding_needed:
        clean_b64 += "=" * (4 - padding_needed)

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

    return json.loads(decrypted_bytes.decode("utf-8"))


def _load_registry() -> dict:
    """Load the encrypted registry from the remote source, using the local cache as a fallback.

    Returns:
        dict: The decrypted registry, or an empty dictionary when both sources are unavailable.

    """
    try:
        cache_buster = int(time.time())
        fresh_url = f"{_CLOUD_URL}?t={cache_buster}"

        req = httpx.get(
            fresh_url,
            headers={"User-Agent": "SpotiFLAC-Agent"},
            timeout=5.0,
        )
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
        logger.warning(
            f"Unable to contact Cloud servers ({e}). Falling back to local cache...",
        )

        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE) as f:
                    cached_string = f.read()
                return _decrypt_base64_payload(cached_string)
        except Exception as cache_e:
            logger.exception(f"Unable to read local cache: {cache_e}")

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


def get_tidal_endpoints(category: str) -> list[str]:
    """Ottiene gli endpoint per Tidal in base alla categoria ('stream', 'post', ecc.)."""
    return _get_registry().get("tidal", {}).get(category, [])


def get_tidal_post_endpoints() -> list[str]:
    """Maintained for backward compatibility."""
    return _get_registry().get("tidal", {}).get("post", [])


def get_deezer_endpoint(key: str) -> str:
    """Retrieve a Deezer endpoint by key.

    Parameters
    ----------
        key (str): Endpoint key, such as `antra`, `s_deezer`, `flacdownloader_prepare`, or `flacdownloader_asset`.

    Returns
    -------
        str: The configured endpoint, or an empty string if the key is unavailable.

    """
    return _get_registry().get("deezer", {}).get(key, "")


def get_amazon_endpoint(key: str) -> str:
    """Valid keys:
    - Download: 'musicdl', 'spotbye1', 'spotbye2', 'zarz', 'zarz_media', 'community', 'antra'
    - S: 's', 's_home', 's_challenge', 's_verify', 's_stream', 's_queue'
    - Resolver: 'resolver_songstats', 'resolver_songlink_api', 'resolver_songlink_html', 'resolver_spotify', 'resolver_deezer'
    - Base: 'amazon_music_base'.
    """
    return _get_registry().get("amazon", {}).get(key, "")


def get_apple_music_endpoint(key: str) -> str:
    """Keys: 'proxy_direct', 'proxy_queued'."""
    return _get_registry().get("apple_music", {}).get(key, "")


def get_asian_provider_endpoint(provider: str, key: str) -> str:
    """For joox, kuwo, migu, netease."""
    return _get_registry().get(provider, {}).get(key, "")


def get_soundcloud_cobalt() -> str:
    return _get_registry().get("soundcloud", {}).get("cobalt", "")


def get_youtube_endpoints(key: str) -> list[str] | str:
    """Keys: 'cobalt', 'zarz_clean', 'zarz_dl'."""
    return _get_registry().get("youtube", {}).get(key, [])


def get_pandora_base_and_path() -> tuple[str, str]:
    pan = _get_registry().get("pandora", {})
    return pan.get("zarz_base", ""), pan.get("zarz_dl", "")


def get_health_zarz_url() -> str:
    return _get_registry().get("health", {}).get("zarz", "")


def get_community_url(provider: str) -> str:
    """Return the Community URL if it exists in the registry, otherwise empty string."""
    return _get_registry().get("community", {}).get(provider, "")




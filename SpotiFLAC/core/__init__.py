from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from SpotiFLAC.core.http import httpx

logger = logging.getLogger(__name__)

_SEED_PARTS = [b"spotif", b"lac:co", b"mmunity:url:v1"]
_AAD = b"spotiflac|community|url|v1"
_CLOUD_URL = "https://gist.githubusercontent.com/BartolomeoRusso9/ef9fdbbc894818aea89d25a8d99f8c77/raw"
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "spotiflac")
_CACHE_FILE = os.path.join(_CACHE_DIR, "endpoints_cache.txt")


def _decrypt_base64_payload(b64_string: str) -> dict:
    clean_b64 = "".join(b64_string.split())
    clean_b64 = clean_b64.replace("-", "+").replace("_", "/")
    clean_b64 = clean_b64.encode("ascii", "ignore").decode("ascii")
    padding_needed = len(clean_b64) % 4
    if padding_needed:
        clean_b64 += "=" * (4 - padding_needed)

    raw_bytes = base64.b64decode(clean_b64)
    nonce = raw_bytes[:12]
    encrypted_payload = raw_bytes[12:]
    hasher = hashlib.sha256()
    for part in _SEED_PARTS:
        hasher.update(part)
    key = hasher.digest()
    decrypted_bytes = AESGCM(key).decrypt(nonce, encrypted_payload, _AAD)
    return json.loads(decrypted_bytes.decode("utf-8"))


def _load_registry() -> dict:
    import tempfile
    try:
        fresh_url = f"{_CLOUD_URL}?t={int(time.time())}"
        response = httpx.get(
            fresh_url,
            headers={"User-Agent": "SpotiFLAC-Agent"},
            timeout=5.0,
        )
        response.raise_for_status()
        cloud_string = response.text
        registry = _decrypt_base64_payload(cloud_string)
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=_CACHE_DIR, prefix=".endpoints_cache_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as cache_file:
                    cache_file.write(cloud_string)
                os.replace(temp_path, _CACHE_FILE)
            except Exception:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                raise
        except Exception:
            pass
        return registry
    except Exception as exc:
        logger.warning(
            "Unable to contact Cloud servers (%s). Falling back to local cache...",
            exc,
        )
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE) as cache_file:
                    return _decrypt_base64_payload(cache_file.read())
        except Exception as cache_exc:
            logger.exception("Unable to read local cache: %s", cache_exc)
        return {}


_TTL_SECONDS = 3600
_registry_cache: dict = {}
_registry_fetched_at = 0.0
_registry_lock = threading.Lock()


def _get_registry() -> dict:
    global _registry_cache, _registry_fetched_at
    with _registry_lock:
        if time.time() - _registry_fetched_at >= _TTL_SECONDS:
            _registry_cache = _load_registry()
            _registry_fetched_at = time.time()
    return _registry_cache


def get_qobuz_endpoints(category: str) -> list[str]:
    return _get_registry().get("qobuz", {}).get(category, [])


def get_tidal_endpoints(category: str) -> list[str]:
    return _get_registry().get("tidal", {}).get(category, [])


def get_tidal_post_endpoints() -> list[str]:
    return _get_registry().get("tidal", {}).get("post", [])


def get_deezer_endpoint(key: str) -> str:
    return _get_registry().get("deezer", {}).get(key, "")


def get_amazon_endpoint(key: str) -> str:
    return _get_registry().get("amazon", {}).get(key, "")


def get_apple_music_endpoint(key: str) -> str:
    return _get_registry().get("apple_music", {}).get(key, "")


def get_asian_provider_endpoint(provider: str, key: str) -> str:
    return _get_registry().get(provider, {}).get(key, "")


def get_soundcloud_cobalt() -> str:
    return _get_registry().get("soundcloud", {}).get("cobalt", "")


def get_youtube_endpoints(key: str) -> list[str] | str:
    return _get_registry().get("youtube", {}).get(key, [])


def get_pandora_base_and_path() -> tuple[str, str]:
    pandora = _get_registry().get("pandora", {})
    return pandora.get("zarz_base", ""), pandora.get("zarz_dl", "")


def get_health_zarz_url() -> str:
    return _get_registry().get("health", {}).get("zarz", "")


def get_community_url(provider: str) -> str:
    return _get_registry().get("community", {}).get(provider, "")


# Registry helpers must be defined before these imports to avoid the package
# initialization cycle through health_check and lyrics.
# ruff: noqa: E402
from .errors import (
    AuthError,
    ErrorKind,
    InvalidUrlError,
    NetworkError,
    ParseError,
    RateLimitedError,
    SpotiflacError,
    TrackNotFoundError,
)
from .health_check import run_health_check
from .http import AsyncHttpClient, AsyncRateLimiter, NetworkManager, RetryConfig
from .lyrics import fetch_lyrics_async
from .metadata_enrichment import enrich_metadata_async
from .models import DownloadResult, TrackMetadata, build_filename, sanitize
from .progress import DownloadManager, ProgressCallback, RichProgressCallback
from .provider_stats import (
    prioritize_async as prioritize_providers_async,
)
from .provider_stats import (
    record_failure_async,
    record_success_async,
)
from .tagger import embed_metadata_async, max_resolution_spotify_cover
from .transcode import transcode_file_async

__all__ = [
    "AsyncHttpClient",
    "AsyncRateLimiter",
    "AuthError",
    "DownloadManager",
    "DownloadResult",
    "ErrorKind",
    "InvalidUrlError",
    "NetworkError",
    "NetworkManager",
    "ParseError",
    "ProgressCallback",
    "RateLimitedError",
    "RetryConfig",
    "RichProgressCallback",
    "SpotiflacError",
    "TrackMetadata",
    "TrackNotFoundError",
    "build_filename",
    "embed_metadata_async",
    "enrich_metadata_async",
    "fetch_lyrics_async",
    "max_resolution_spotify_cover",
    "prioritize_providers_async",
    "record_failure_async",
    "record_success_async",
    "run_health_check",
    "sanitize",
    "transcode_file_async",
    "get_amazon_endpoint",
    "get_apple_music_endpoint",
    "get_asian_provider_endpoint",
    "get_community_url",
    "get_deezer_endpoint",
    "get_health_zarz_url",
    "get_pandora_base_and_path",
    "get_qobuz_endpoints",
    "get_soundcloud_cobalt",
    "get_tidal_endpoints",
    "get_tidal_post_endpoints",
    "get_youtube_endpoints",
]

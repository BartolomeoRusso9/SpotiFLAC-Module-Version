from __future__ import annotations
import time
import struct
import hmac
import hashlib
import logging
import httpx

logger = logging.getLogger(__name__)

# Fallback secrets
_TOTP_VERSION = 61
_TOTP_SECRETS: dict[int, list[int]] = {
    59: [123,105,79,70,110,59,52,125,60,49,80,70,89,75,80,86,63,53,123,37,117,49,52,93,77,62,47,86,48,104,68,72],
    60: [79,109,69,123,90,65,46,74,94,34,58,48,70,71,92,85,122,63,91,64,87,87],
    61: [44,55,47,42,70,40,34,114,76,74,50,111,120,97,75,76,94,102,43,69,49,120,118,80,64,78],
}

_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_CACHED_SECRETS = None

def get_secrets() -> dict[int, list[int]]:
    """Dynamically fetches the latest TOTP secrets from the community repository."""
    global _CACHED_SECRETS
    if _CACHED_SECRETS is not None:
        return _CACHED_SECRETS
    
    try:
        url = "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/main/secrets/secretDict.json"
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            _CACHED_SECRETS = {int(k): v for k, v in data.items()}
            return _CACHED_SECRETS
    except Exception as exc:
        logger.warning(f"[spotify_totp] Could not fetch remote TOTP secrets: {exc}")
    
    # Fallback to local hardcoded secrets if offline or fails
    return _TOTP_SECRETS

def _base32_encode(data: bytes) -> str:
    result = []
    bits = 0
    value = 0
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            result.append(_BASE32_ALPHABET[(value >> (bits - 5)) & 31])
            bits -= 5
    if bits > 0:
        result.append(_BASE32_ALPHABET[(value << (5 - bits)) & 31])
    return "".join(result)

def _base32_decode(s: str) -> bytes:
    s = s.upper().rstrip("=")
    result = []
    bits = 0
    value = 0
    for ch in s:
        idx = _BASE32_ALPHABET.find(ch)
        if idx == -1:
            continue
        value = (value << 5) | idx
        bits += 5
        if bits >= 8:
            result.append((value >> (bits - 8)) & 0xFF)
            bits -= 8
    return bytes(result)

def _hotp(key_bytes: bytes, counter: int) -> str:
    counter_bytes = struct.pack(">Q", counter)
    h = hmac.new(key_bytes, counter_bytes, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (
            ((h[offset] & 0x7F) << 24)
            | ((h[offset + 1] & 0xFF) << 16)
            | ((h[offset + 2] & 0xFF) << 8)
            | (h[offset + 3] & 0xFF)
    )
    return str(code % 1_000_000).zfill(6)

def _compute_secret(version: int, secrets_dict: dict[int, list[int]]) -> str:
    secret_list = secrets_dict.get(version)
    if not secret_list:
        # Fallback to highest available if requested version not found
        version = max(secrets_dict.keys())
        secret_list = secrets_dict[version]

    # Step 1: XOR each value with ((i % 33) + 9)
    transformed = [v ^ ((i % 33) + 9) for i, v in enumerate(secret_list)]

    # Step 2: every number -> decimal string, concatenate
    joined = "".join(str(n) for n in transformed)

    # Step 3: every character of string -> its ASCII code in hex
    hex_str = "".join(format(ord(ch), "02x") for ch in joined)

    # Step 4: hex string -> bytes
    hex_bytes = bytes(int(hex_str[i:i+2], 16) for i in range(0, len(hex_str), 2))

    # Step 5: base32 encode
    return _base32_encode(hex_bytes)

def generate_spotify_totp(
        timestamp: float | None = None,
        version: int | None = None,
) -> tuple[str, int]:
    """
    Generates a Spotify TOTP code dynamically.
    """
    try:
        secrets = get_secrets()
        if version is None:
            # Dynamically determine the highest current version directly from API
            version = max(secrets.keys())
            
        ts = timestamp if timestamp is not None else time.time()
        counter = int(ts) // 30

        secret_b32 = _compute_secret(version, secrets)
        key_bytes = _base32_decode(secret_b32)
        code = _hotp(key_bytes, counter)
        return code, version
    except Exception as exc:
        logger.error("[spotify_totp] Code generation error: %s", exc)
        return "", (version or _TOTP_VERSION)
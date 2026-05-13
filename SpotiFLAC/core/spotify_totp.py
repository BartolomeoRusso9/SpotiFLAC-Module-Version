"""
Spotify TOTP Generator — allineato esattamente a index.js v61.

La logica segue la stessa pipeline del JS:
  1. secret_list  → XOR con ((i % 33) + 9)
  2. Ogni numero trasformato → stringa decimale, concatenate
  3. Stringa → codici ASCII in hex
  4. Hex bytes → base32
  5. Base32 → codice TOTP standard (30 s, 6 cifre)
"""
from __future__ import annotations
import time
import struct
import hmac
import hashlib
import logging

logger = logging.getLogger(__name__)

# Secret array per la versione 61 (da index.js TOTP_SECRETS[61])
_TOTP_VERSION = 61
_TOTP_SECRETS: dict[int, list[int]] = {
    59: [123,105,79,70,110,59,52,125,60,49,80,70,89,75,80,86,63,53,123,37,117,49,52,93,77,62,47,86,48,104,68,72],
    60: [79,109,69,123,90,65,46,74,94,34,58,48,70,71,92,85,122,63,91,64,87,87],
    61: [44,55,47,42,70,40,34,114,76,74,50,111,120,97,75,76,94,102,43,69,49,120,118,80,64,78],
}

_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


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


def _compute_secret(version: int) -> str:
    """Riproduce esattamente la pipeline di index.js per trasformare il secret array in base32."""
    secret_list = _TOTP_SECRETS.get(version, _TOTP_SECRETS[_TOTP_VERSION])

    # Step 1: XOR ciascun valore con ((i % 33) + 9)
    transformed = [v ^ ((i % 33) + 9) for i, v in enumerate(secret_list)]

    # Step 2: ogni numero → stringa decimale, concatenate
    joined = "".join(str(n) for n in transformed)

    # Step 3: ogni CARATTERE della stringa → il suo codice ASCII in hex (2 cifre)
    hex_str = "".join(format(ord(ch), "02x") for ch in joined)

    # Step 4: hex string → bytes
    hex_bytes = bytes(int(hex_str[i:i+2], 16) for i in range(0, len(hex_str), 2))

    # Step 5: base32 encode
    return _base32_encode(hex_bytes)


def generate_spotify_totp(
        timestamp: float | None = None,
        version: int = _TOTP_VERSION,
) -> tuple[str, int]:
    """
    Genera un codice TOTP Spotify e restituisce (codice, versione).

    Non richiede alcuna dipendenza esterna (no pyotp) — usa solo stdlib.
    """
    try:
        ts = timestamp if timestamp is not None else time.time()
        counter = int(ts) // 30

        secret_b32 = _compute_secret(version)
        key_bytes = _base32_decode(secret_b32)
        code = _hotp(key_bytes, counter)
        return code, version
    except Exception as exc:
        logger.error("[spotify_totp] Errore nella generazione del codice: %s", exc)
        return "", version


if __name__ == "__main__":
    code, ver = generate_spotify_totp()
    print(f"Codice: {code}, Versione: {ver}")
"""
Spotify TOTP Generator — port di spotify_totp.go.
Genera i codici temporanei necessari per l'autenticazione anonima ai mirror Spotify.
"""
from __future__ import annotations
import time
import pyotp
import logging

logger = logging.getLogger(__name__)

# Segreto e versione allineati alla versione Go
_SPOTIFY_TOTP_SECRET = (
    "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
)
_SPOTIFY_TOTP_VERSION = 61

def generate_spotify_totp(timestamp: float | None = None) -> tuple[str, int]:
    """
    Genera un codice TOTP valido per Spotify e restituisce la versione del protocollo.

    Args:
        timestamp: Il tempo per cui generare il codice (default: ora attuale).

    Returns:
        Una tupla contenente (codice_totp, versione_protocollo).
    """
    try:
        if timestamp is None:
            timestamp = time.time()

        # Implementazione TOTP standard (30s interval, 6 digits) come in Go
        totp = pyotp.TOTP(_SPOTIFY_TOTP_SECRET)
        code = totp.at(timestamp)

        return code, _SPOTIFY_TOTP_VERSION
    except Exception as e:
        logger.error("[spotify_totp] Errore nella generazione del codice: %s", e)
        # Ritorna un valore vuoto o gestibile in caso di errore critico
        return "", _SPOTIFY_TOTP_VERSION

if __name__ == "__main__":
    # Test veloce
    codice, versione = generate_spotify_totp()
    print(f"Codice: {codice}, Versione: {versione}")
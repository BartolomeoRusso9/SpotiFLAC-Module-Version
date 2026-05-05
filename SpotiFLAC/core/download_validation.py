# SpotiFLAC/core/download_validation.py
"""
Port di download_validation.go — rileva preview da 30s e mismatch di durata.
"""
from __future__ import annotations
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_PREVIEW_MAX_SECONDS      = 35
_PREVIEW_EXPECTED_MIN     = 60
_LARGE_MISMATCH_MIN       = 90
_MIN_ALLOWED_DIFF         = 15
_DURATION_DIFF_RATIO      = 0.25


def _get_audio_duration(filepath: str) -> float:
    """Usa ffprobe per ottenere la durata in secondi."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", filepath],
            capture_output=True, text=True,
        )
        import json
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


def validate_downloaded_track(
    filepath:         str,
    expected_seconds: int,
) -> tuple[bool, str]:
    """
    Controlla che il file scaricato non sia una preview da 30s.
    Ritorna (valido, messaggio_errore).
    Equivalente a ValidateDownloadedTrackDuration() del Go.
    """
    if not filepath or expected_seconds <= 0:
        return True, ""

    actual = _get_audio_duration(filepath)
    if actual <= 0:
        return True, ""

    actual_s = round(actual)

    # Caso 1: preview da 30s su brano lungo
    if expected_seconds >= _PREVIEW_EXPECTED_MIN and actual_s <= _PREVIEW_MAX_SECONDS:
        msg = (
            f"Preview rilevata: file è {actual_s}s, "
            f"attesi ~{expected_seconds}s — file rimosso"
        )
        _remove_file(filepath)
        return False, msg

    # Caso 2: mismatch grande su brani lunghi
    if expected_seconds >= _LARGE_MISMATCH_MIN:
        allowed = max(_MIN_ALLOWED_DIFF,
                      round(expected_seconds * _DURATION_DIFF_RATIO))
        diff = abs(actual_s - expected_seconds)
        if diff > allowed:
            msg = (
                f"Durata errata: file è {actual_s}s, "
                f"attesi ~{expected_seconds}s — file rimosso"
            )
            _remove_file(filepath)
            return False, msg

    if expected_seconds > 0 and expected_seconds < _PREVIEW_EXPECTED_MIN:
        # Se il file scaricato dura meno del 60% della durata attesa, è chiaramente troncato
        if actual_s < (expected_seconds * 0.6):
            msg = (
                f"Durata errata (brano corto troncato): file è {actual_s}s, "
                f"attesi ~{expected_seconds}s — file rimosso"
            )
            _remove_file(filepath)
            return False, msg
    return True, ""


def _remove_file(filepath: str) -> None:
    try:
        os.remove(filepath)
        logger.warning("[validation] File rimosso: %s", filepath)
    except OSError as exc:
        logger.warning("[validation] Impossibile rimuovere %s: %s", filepath, exc)
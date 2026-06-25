# backend/core/download_validation.py
from __future__ import annotations
import logging
import os
import asyncio
import json

logger = logging.getLogger(__name__)

_PREVIEW_MAX_SECONDS = 35
_PREVIEW_EXPECTED_MIN = 60
_LARGE_MISMATCH_MIN = 90
_MIN_ALLOWED_DIFF = 15
_DURATION_DIFF_RATIO = 0.25


async def _get_audio_duration_async(filepath: str) -> float:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        data = json.loads(stdout.decode("utf-8"))
        return float(data.get("format", {}).get("duration", 0))
    except Exception as exc:
        logger.warning("[validation] No ffprobe found or error. Error: %s", exc)
        return 0.0


async def _remove_file_async(filepath: str) -> None:
    try:
        # File removal is blocking I/O; delegate it to a thread
        await asyncio.to_thread(os.remove, filepath)
        logger.warning("[validation] File removed: %s", filepath)
    except OSError as exc:
        logger.warning("[validation] Unable to remove %s: %s", filepath, exc)


async def validate_downloaded_track_async(
    filepath: str,
    expected_seconds: int,
) -> tuple[bool, str]:
    """
    Check che il file sloaded non sia una preview da 30s.
    Returns (valido, messaggio_errore).
    Equivalent a ValidateDownloadedTrackDuration() del Go.
    """
    if not filepath or expected_seconds <= 0:
        return True, ""

    actual = await _get_audio_duration_async(filepath)
    if actual <= 0:
        return True, ""

    actual_s = round(actual)

    # Caso 1: preview da 30s su brano lungo
    if expected_seconds >= _PREVIEW_EXPECTED_MIN and actual_s <= _PREVIEW_MAX_SECONDS:
        msg = (
            f"Preview: file is {actual_s}s, "
            f"expected ~{expected_seconds}s — file rimosso"
        )
        await _remove_file_async(filepath)
        return False, msg

    # Caso 2: mismatch grande su brani lunghi
    if expected_seconds >= _LARGE_MISMATCH_MIN:
        allowed = max(_MIN_ALLOWED_DIFF, round(expected_seconds * _DURATION_DIFF_RATIO))
        diff = abs(actual_s - expected_seconds)
        if diff > allowed:
            msg = (
                f"Durata errata: file is {actual_s}s, "
                f"expected ~{expected_seconds}s — file rimosso"
            )
            await _remove_file_async(filepath)
            return False, msg

    if expected_seconds > 0 and expected_seconds < _PREVIEW_EXPECTED_MIN:
        if actual_s < (expected_seconds * 0.6):
            msg = (
                f"Durata errata (brano corto troncato): file is {actual_s}s, "
                f"expected ~{expected_seconds}s — file rimosso"
            )
            await _remove_file_async(filepath)
            return False, msg

    return True, ""

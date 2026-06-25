# flac_validation.py
"""
FLAC file validation and repair utilities.
Detects and fixes corrupted FLAC files, especially those from Amazon provider
with FLAC__STREAM_DECODER_ERROR_STATUS_LOST_SYNC issues.
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

def _ffmpeg_path() -> str:
    return "ffmpeg"

def _ffprobe_path() -> str:
    return "ffprobe"

def validate_flac_file(filepath: str) -> tuple[bool, str]:
    """
    Validates a FLAC file by checking its integrity.
    Returns (is_valid, error_message).
    
    Uses ffmpeg to validate the FLAC stream can be decoded.
    """
    if not os.path.exists(filepath):
        return False, "File does not exist"
    
    if not filepath.lower().endswith(".flac"):
        return True, ""  # Not a FLAC file, skip validation
    
    try:
        # Try to decode the FLAC file with ffmpeg
        result = subprocess.run(
            [_ffmpeg_path(), "-v", "error", "-i", filepath, "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            if "FLAC__STREAM_DECODER_ERROR" in error_msg or "sync" in error_msg.lower():
                return False, f"FLAC sync error: {error_msg[:100]}"
            return False, f"FLAC validation failed: {error_msg[:100]}"
        
        return True, ""
    
    except subprocess.TimeoutExpired:
        return False, "FLAC validation timeout"
    except Exception as exc:
        logger.warning("[flac_validation] Validation error: %s", exc)
        return False, str(exc)

def repair_flac_file(input_path: str, output_path: str = None) -> tuple[bool, str]:
    """
    Attempts to repair a corrupted FLAC file by re-encoding it with ffmpeg.
    
    Args:
        input_path: Path to corrupted FLAC file
        output_path: Path for repaired file (uses input_path if None)
    
    Returns:
        (success, message)
    """
    if not os.path.exists(input_path):
        return False, "Input file does not exist"
    
    if output_path is None:
        # Use a temp file and replace original
        output_path = input_path + ".repaired"
        replace_original = True
    else:
        replace_original = False
    
    try:
        logger.info("[flac_validation] Attempting to repair FLAC file: %s", input_path)
        
        si = None
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        # Use ffmpeg to re-encode the FLAC file
        # This will skip corrupted frames and reconstruct the stream
        result = subprocess.run(
            [_ffmpeg_path(), "-y", "-i", input_path, "-c:a", "flac", "-q:a", "8", output_path],
            capture_output=True,
            text=True,
            timeout=300,
            startupinfo=si,
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            logger.warning("[flac_validation] Repair failed: %s", error_msg[:200])
            if os.path.exists(output_path):
                os.remove(output_path)
            return False, f"Repair failed: {error_msg[:100]}"
        
        # Validate the repaired file
        is_valid, _ = validate_flac_file(output_path)
        if not is_valid:
            if os.path.exists(output_path):
                os.remove(output_path)
            return False, "Repaired file still invalid"
        
        # Replace original if needed
        if replace_original:
            try:
                os.remove(input_path)
                os.rename(output_path, input_path)
                logger.info("[flac_validation] FLAC file successfully repaired: %s", input_path)
                return True, "File repaired successfully"
            except OSError as exc:
                logger.warning("[flac_validation] Failed to replace original: %s", exc)
                return False, f"Failed to replace original: {exc}"
        
        logger.info("[flac_validation] FLAC file successfully repaired: %s", output_path)
        return True, "File repaired successfully"
    
    except subprocess.TimeoutExpired:
        if os.path.exists(output_path):
            os.remove(output_path)
        return False, "Repair timeout"
    except Exception as exc:
        logger.warning("[flac_validation] Repair error: %s", exc)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        return False, str(exc)

def validate_and_repair_if_needed(filepath: str) -> tuple[bool, str]:
    """
    Validates a FLAC file and automatically repairs it if corrupted.
    
    Returns:
        (success, message)
    """
    if not filepath.lower().endswith(".flac"):
        return True, ""
    
    # First validate
    is_valid, error_msg = validate_flac_file(filepath)
    if is_valid:
        return True, ""
    
    logger.warning("[flac_validation] FLAC file is corrupted, attempting repair: %s", error_msg)
    
    # Try to repair
    success, repair_msg = repair_flac_file(filepath)
    if success:
        return True, repair_msg
    
    logger.error("[flac_validation] Failed to repair FLAC file: %s", repair_msg)
    return False, repair_msg

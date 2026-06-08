from __future__ import annotations
import subprocess
import logging

logger = logging.getLogger(__name__)

_DOWNLOAD_URL = "https://ffmpeg.org/download.html"


def check_ffmpeg() -> dict:
    """
    Ritorna dict con chiavi:
      available (bool), version (str), error (str)
    """
    result = {"available": False, "version": "", "error": ""}
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            result["available"] = True
            result["version"] = proc.stdout.split("\n")[0].strip()
        else:
            result["error"] = "ffmpeg returned non-zero exit code"
    except FileNotFoundError:
        result["error"] = "ffmpeg not found in PATH"
    except subprocess.TimeoutExpired:
        result["error"] = "ffmpeg check timed out"
    except Exception as exc:
        result["error"] = str(exc)

    if result["available"]:
        logger.debug("[ffmpeg] Found: %s", result["version"])
    else:
        logger.warning("[ffmpeg] Not available: %s", result["error"])

    return result


def print_ffmpeg_warning(result: dict | None = None) -> dict:
    """Stampa avviso CLI se ffmpeg manca. Ritorna il dict del check."""
    if result is None:
        result = check_ffmpeg()

    if result["available"]:
        short = result["version"][:60]
        print(f"  ✓  ffmpeg: {short}")
        return result

    w = 62
    lines = [
        f"⚠  ffmpeg NOT FOUND — some providers will fail",
        f"   · Tidal FLAC muxing / Dolby Atmos",
        f"   · Amazon Music stream decryption",
        f"",
        f"   Error:    {result['error']}",
        f"   Download: {_DOWNLOAD_URL}",
    ]
    print(f"\n ╭{'─' * w}╮")
    for line in lines:
        print(f" │ {line:<{w}} │")
    print(f" ╰{'─' * w}╯\n")

    return result
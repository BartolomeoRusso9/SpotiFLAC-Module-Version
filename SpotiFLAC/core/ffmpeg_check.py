"""ffmpeg availability check + best-effort auto-install for transcoding.

Mirrors core/node_check.py's shape (check_ffmpeg() / print_ffmpeg_warning()
for the startup check, plus ensure_ffmpeg_installed()): a best-effort install
via whatever system package manager is actually on PATH. Unlike Node.js,
ffmpeg is only ever auto-installed for one specific, opt-in workflow —
MP3 transcoding (see core/transcode.py's ensure_ffmpeg_available(), the sole
caller) — so it stays lazy in the exact same sense: nothing is attempted
until a run actually asks for it.

Tidal-style segment muxing and Amazon-style decryption also depend on
ffmpeg (see extensions/provider.py), but those run deep inside a per-track
pipeline with no single "about to start" gate to hook into, so they are
intentionally left as before: a missing ffmpeg still fails those the same
way it always did, reported via the startup check below.

Design choices worth knowing (identical reasoning to node_check.py):
  - Never escalates privileges itself (no sudo/runas is ever prepended).
    On Linux the plain package-manager command is attempted as-is; if that
    fails because it needs root, the failure is reported with the exact
    command to run manually (with sudo) rather than silently retrying
    elevated. This matters both because escalating privileges without
    being asked is not something to do quietly, and because Docker's root
    user (this project's own image) needs no elevation at all — the same
    plain command already succeeds there.
  - Every install command is passed non-interactively (-y / --noconfirm /
    --accept-*-agreements) so it can never sit waiting on a prompt with
    nothing attached to answer it.
  - Bounded by a timeout (install can be slow — first-time apt-get update,
    a cold winget cache, ...) so a stuck installer can't hang the caller
    forever; the caller (ensure_ffmpeg_available()) already accepts this
    may take a while as the cost of "first transcode of the run".
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)

_DOWNLOAD_URL = "https://ffmpeg.org/download.html"
_INSTALL_TIMEOUT_S = 300  # 5 minutes — package installs can be genuinely slow


def check_ffmpeg() -> dict:
    """Returns dict with keys: available (bool), version (str), error (str)."""
    result = {"available": False, "version": "", "error": ""}
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=5,
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


def _package_manager_command() -> tuple[str, list[str]] | None:
    """Returns (manager_name, install_argv) for the first package manager
    found on PATH for the current OS, or None if none of the documented
    ones are available. Every command is non-interactive on purpose — see
    the module docstring. Mirrors node_check._package_manager_command().
    """
    system = platform.system()

    if system == "Linux":
        candidates = [
            ("apt-get", ["apt-get", "install", "-y", "ffmpeg"]),
            ("dnf", ["dnf", "install", "-y", "ffmpeg"]),
            ("yum", ["yum", "install", "-y", "ffmpeg"]),
            ("pacman", ["pacman", "-Sy", "--noconfirm", "ffmpeg"]),
        ]
    elif system == "Darwin":
        candidates = [("brew", ["brew", "install", "ffmpeg"])]
    elif system == "Windows":
        candidates = [
            (
                "winget",
                [
                    "winget",
                    "install",
                    "--id",
                    "Gyan.FFmpeg",
                    "-e",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
            ),
            ("choco", ["choco", "install", "ffmpeg", "-y"]),
        ]
    else:
        candidates = []

    for name, argv in candidates:
        if shutil.which(name):
            return name, argv
    return None


def _manual_install_hint() -> str:
    system = platform.system()
    if system == "Linux":
        return (
            "Install ffmpeg yourself, e.g. one of:\n"
            "    sudo apt-get install -y ffmpeg   (Debian/Ubuntu)\n"
            "    sudo dnf install -y ffmpeg        (Fedora — may need RPM Fusion)\n"
            "    sudo yum install -y ffmpeg        (RHEL/CentOS — may need EPEL/RPM Fusion)\n"
            "    sudo pacman -Sy ffmpeg            (Arch)\n"
            f"or download it from {_DOWNLOAD_URL}"
        )
    if system == "Darwin":
        return (
            "Install ffmpeg yourself, e.g.:\n"
            "    brew install ffmpeg\n"
            f"or download it from {_DOWNLOAD_URL}"
        )
    if system == "Windows":
        return (
            "Install ffmpeg yourself, e.g. one of:\n"
            "    winget install --id Gyan.FFmpeg -e\n"
            "    choco install ffmpeg\n"
            f"or download it from {_DOWNLOAD_URL}"
        )
    return f"Install ffmpeg from {_DOWNLOAD_URL}"


def ensure_ffmpeg_installed(*, print_progress: bool = True) -> dict:
    """If ffmpeg isn't found, attempts a best-effort install via whatever
    system package manager is on PATH for this OS (see the MP3 Transcoding
    section of the README for the supported list), printing progress the
    whole way through since this can take a while and would otherwise look
    like SpotiFLAC hanging.

    Always returns the same shape as check_ffmpeg() — call it again after
    this to see whether it actually worked. Never raises: a failed install
    attempt is reported in the returned dict's "error", not as an exception,
    so the caller can fall back to its own error message either way.

    Mirrors node_check.ensure_node_installed() exactly, including every
    safety guarantee described in the module docstring.
    """

    def _say(msg: str) -> None:
        if print_progress:
            print(msg)
        logger.info(msg)

    result = check_ffmpeg()
    if result["available"]:
        return result

    picked = _package_manager_command()
    if picked is None:
        result["error"] = (
            f"ffmpeg not found and no supported package manager is on "
            f"PATH for this OS. {_manual_install_hint()}"
        )
        return result

    manager_name, argv = picked
    _say(
        f"[ffmpeg] ffmpeg not found — attempting to install it automatically "
        f"via {manager_name} (this can take a few minutes the first time)…"
    )

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        result["error"] = (
            f"Timed out after {_INSTALL_TIMEOUT_S}s waiting for `{manager_name}` "
            f"to install ffmpeg. {_manual_install_hint()}"
        )
        _say(f"[ffmpeg] {result['error']}")
        return result
    except Exception as exc:
        result["error"] = (
            f"Failed to run {manager_name}: {exc}. {_manual_install_hint()}"
        )
        _say(f"[ffmpeg] {result['error']}")
        return result

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        needs_root = (
            platform.system() == "Linux"
            and hasattr(os, "geteuid")
            and os.geteuid() != 0
        )
        sudo_hint = (
            f" This usually needs root — try: sudo {' '.join(argv)}"
            if needs_root
            else ""
        )
        result["error"] = (
            f"{manager_name} exited with code {proc.returncode}: "
            f"{stderr_tail[0]}.{sudo_hint} {_manual_install_hint()}"
        )
        _say(f"[ffmpeg] Automatic install failed. {result['error']}")
        return result

    _say(f"[ffmpeg] {manager_name} finished. Verifying ffmpeg is now available…")
    result = check_ffmpeg()
    if result["available"]:
        _say(f"[ffmpeg] ffmpeg installed successfully: {result['version']}")
    else:
        result["error"] = (
            f"{manager_name} reported success but `ffmpeg` still isn't on "
            f"PATH — you may need to restart your terminal/shell for the "
            f"change to take effect. {_manual_install_hint()}"
        )
        _say(f"[ffmpeg] {result['error']}")
    return result


def print_ffmpeg_warning(result: dict | None = None) -> dict:
    """Prints a CLI warning if ffmpeg is missing. Returns the check dict."""
    if result is None:
        result = check_ffmpeg()

    if result["available"]:
        return result

    lines = [
        "⚠  ffmpeg NOT FOUND  ⚠",
        "",
        f"   Error:    {result['error']}",
        "   Tidal FLAC muxing and Amazon decryption will fail until it's",
        "   installed. MP3 transcoding (--mp3) will try to install it",
        "   automatically when you first use it; otherwise install it",
        "   yourself:",
        f"   {_DOWNLOAD_URL}",
    ]
    for _line in lines:
        print(_line)

    return result

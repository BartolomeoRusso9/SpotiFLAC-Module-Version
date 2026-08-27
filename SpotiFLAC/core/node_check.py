"""Node.js availability check + best-effort auto-install for JS extensions.

Mirrors core/ffmpeg_check.py's shape (check_node() / print_node_warning())
for the startup check, plus ensure_node_installed(): a best-effort install
via whatever system package manager is actually on PATH, matching the
README's documented behavior ("the first time a JavaScript extension is
used") — called lazily from extensions/runtime.py's JSRuntime.start(),
not eagerly at every app startup.

Design choices worth knowing:
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
    forever; the caller (JSRuntime.start()) already accepts this may take
    a while as the cost of "first extension run".
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)

_DOWNLOAD_URL = "https://nodejs.org/en/download"
_INSTALL_TIMEOUT_S = 300  # 5 minutes — package installs can be genuinely slow


def check_node(node_executable: str = "node") -> dict:
    """Returns dict with keys: available (bool), version (str), error (str).
    Mirrors ffmpeg_check.check_ffmpeg()'s shape exactly.
    """
    result = {"available": False, "version": "", "error": ""}
    try:
        proc = subprocess.run(
            [node_executable, "-v"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            result["available"] = True
            result["version"] = proc.stdout.strip().split("\n")[0].strip()
        else:
            result["error"] = "node returned non-zero exit code"
    except FileNotFoundError:
        result["error"] = "node not found in PATH"
    except subprocess.TimeoutExpired:
        result["error"] = "node check timed out"
    except Exception as exc:
        result["error"] = str(exc)

    if result["available"]:
        logger.debug("[node] Found: %s", result["version"])
    else:
        logger.warning("[node] Not available: %s", result["error"])

    return result


def _package_manager_command() -> tuple[str, list[str]] | None:
    """Returns (manager_name, install_argv) for the first package manager
    found on PATH for the current OS, or None if none of the documented
    ones are available. Every command is non-interactive on purpose — see
    the module docstring.
    """
    system = platform.system()

    if system == "Linux":
        candidates = [
            ("apt-get", ["apt-get", "install", "-y", "nodejs"]),
            ("dnf", ["dnf", "install", "-y", "nodejs"]),
            ("yum", ["yum", "install", "-y", "nodejs"]),
            ("pacman", ["pacman", "-Sy", "--noconfirm", "nodejs"]),
        ]
    elif system == "Darwin":
        candidates = [("brew", ["brew", "install", "node"])]
    elif system == "Windows":
        candidates = [
            (
                "winget",
                [
                    "winget",
                    "install",
                    "--id",
                    "OpenJS.NodeJS",
                    "-e",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
            ),
            ("choco", ["choco", "install", "nodejs", "-y"]),
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
            "Install Node.js yourself, e.g. one of:\n"
            "    sudo apt-get install -y nodejs   (Debian/Ubuntu)\n"
            "    sudo dnf install -y nodejs        (Fedora)\n"
            "    sudo yum install -y nodejs        (RHEL/CentOS)\n"
            "    sudo pacman -Sy nodejs            (Arch)\n"
            f"or download it from {_DOWNLOAD_URL}"
        )
    if system == "Darwin":
        return (
            "Install Node.js yourself, e.g.:\n"
            "    brew install node\n"
            f"or download it from {_DOWNLOAD_URL}"
        )
    if system == "Windows":
        return (
            "Install Node.js yourself, e.g. one of:\n"
            "    winget install --id OpenJS.NodeJS -e\n"
            "    choco install nodejs\n"
            f"or download it from {_DOWNLOAD_URL}"
        )
    return f"Install Node.js ≥ 16 from {_DOWNLOAD_URL}"


def ensure_node_installed(
    node_executable: str = "node", *, print_progress: bool = True
) -> dict:
    """If `node_executable` isn't found, attempts a best-effort install via
    whatever system package manager is on PATH for this OS (see the
    README's Extensions section for the supported list), printing progress
    the whole way through since this can take a while and would otherwise
    look like SpotiFLAC hanging.

    Always returns the same shape as check_node() — call it again after
    this to see whether it actually worked. Never raises: a failed install
    attempt is reported in the returned dict's "error", not as an exception,
    so the caller can fall back to its own error message either way.
    """

    def _say(msg: str) -> None:
        if print_progress:
            print(msg)
        logger.info(msg)

    result = check_node(node_executable)
    if result["available"]:
        return result

    picked = _package_manager_command()
    if picked is None:
        result["error"] = (
            f"Node.js not found and no supported package manager is on "
            f"PATH for this OS. {_manual_install_hint()}"
        )
        return result

    manager_name, argv = picked
    _say(
        f"[node] Node.js not found — attempting to install it automatically "
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
            f"to install Node.js. {_manual_install_hint()}"
        )
        _say(f"[node] {result['error']}")
        return result
    except Exception as exc:
        result["error"] = (
            f"Failed to run {manager_name}: {exc}. {_manual_install_hint()}"
        )
        _say(f"[node] {result['error']}")
        return result

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        needs_root = platform.system() == "Linux" and os.geteuid() != 0
        sudo_hint = (
            f" This usually needs root — try: sudo {' '.join(argv)}"
            if needs_root
            else ""
        )
        result["error"] = (
            f"{manager_name} exited with code {proc.returncode}: "
            f"{stderr_tail[0]}.{sudo_hint} {_manual_install_hint()}"
        )
        _say(f"[node] Automatic install failed. {result['error']}")
        return result

    _say(f"[node] {manager_name} finished. Verifying Node.js is now available…")
    result = check_node(node_executable)
    if result["available"]:
        _say(f"[node] Node.js installed successfully: {result['version']}")
    else:
        result["error"] = (
            f"{manager_name} reported success but `{node_executable}` still "
            f"isn't on PATH — you may need to restart your terminal/shell "
            f"for the change to take effect. {_manual_install_hint()}"
        )
        _say(f"[node] {result['error']}")
    return result


def print_node_warning(result: dict | None = None) -> dict:
    """Prints a CLI warning if Node.js is missing. Returns the check dict.
    Mirrors ffmpeg_check.print_ffmpeg_warning()'s shape exactly.
    """
    if result is None:
        result = check_node()

    if result["available"]:
        return result

    lines = [
        "⚠  Node.js NOT FOUND  ⚠",
        "",
        f"   Error:   {result['error']}",
        "   JavaScript extensions won't work until Node.js is installed —",
        "   SpotiFLAC will try to install it automatically the first time",
        "   you use one (see the Extensions section of the README for the",
        "   supported package managers), or install it yourself:",
        f"   {_DOWNLOAD_URL}",
    ]
    for _line in lines:
        print(_line)

    return result

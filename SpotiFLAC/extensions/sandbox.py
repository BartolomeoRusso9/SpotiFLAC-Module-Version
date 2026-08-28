"""Limits on the environment a JavaScript extension runs in.

The problem
-----------
Extensions come from whatever registry the operator configured, and the
project is explicit that it neither reviews nor controls them. They already
run in their own `node` process — but that process was started with
`os.environ.copy()`, which hands every third-party extension the host's
entire environment. On a machine that has ever exported one, that includes:

    SPOTIFLAC_WEB_TOKEN, SPOTIFLAC_LIBRARY_TOKEN, PYPI_API_TOKEN,
    AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, DATABASE_URL, ...

None of which an extension that fetches a track has any use for, and all of
which `process.env` hands over for free.

What this does
--------------
Builds the child environment from an allowlist instead of by inheritance,
and — on POSIX — applies address-space, CPU and file-size limits so a
runaway extension exhausts its own budget rather than the machine's.

What this is not
----------------
Not a security boundary. The extension still runs as the same OS user, can
still open sockets and write files, and a genuinely hostile one is limited
only by what that user can do. This removes the accidental handover of
credentials and caps obvious resource exhaustion; it does not contain an
attacker. Real isolation needs a container or a separate user account, and
saying otherwise would be worse than saying nothing.

Python extensions get none of this: they are imported into this process
(see python_provider.py), so they share its memory and its environment by
construction. Nothing short of moving them to a subprocess changes that.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Names an extension process is given, if the host has them. Everything
#: else is dropped. Chosen as "what a program that makes HTTPS requests
#: needs to work at all", not "what looks harmless".
_BASE_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    # Node's own knobs, including the OpenSSL legacy flag runtime.py sets.
    "NODE_OPTIONS",
    "NODE_EXTRA_CA_CERTS",
    "NODE_PATH",
    # Corporate proxies and custom CA bundles: without these, an extension
    # simply cannot reach the internet on a lot of real networks.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)

#: Windows falls over without these — they are how the OS locates its own
#: DLLs and the temp directory, not user configuration.
_WINDOWS_ALLOWLIST = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)

#: Escape hatch for an extension that genuinely needs something else:
#: SPOTIFLAC_EXT_ENV_PASSTHROUGH="MY_API_KEY,OTHER_VAR". Deliberately
#: opt-in and per-name — the operator says exactly what to hand over.
PASSTHROUGH_ENV = "SPOTIFLAC_EXT_ENV_PASSTHROUGH"

#: Off switch, for anyone whose extensions broke and who would rather have
#: the old behaviour than debug it right now.
DISABLE_ENV = "SPOTIFLAC_EXT_NO_SANDBOX"

#: Per-process caps. Generous — a provider decrypting a FLAC in memory is a
#: legitimate few hundred MB — but far below "take the machine down".
DEFAULT_MEMORY_MB = 2048
DEFAULT_CPU_SECONDS = 900
DEFAULT_FILE_SIZE_MB = 4096


def sandbox_disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "y")


def _passthrough_names() -> tuple[str, ...]:
    raw = os.environ.get(PASSTHROUGH_ENV, "")
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment to start an extension process with.

    `extra` is merged last, for values the caller computed itself (runtime.py
    sets NODE_OPTIONS this way).
    """
    if sandbox_disabled():
        env = os.environ.copy()
        env.update(extra or {})
        return env

    allowed = list(_BASE_ALLOWLIST)
    if os.name == "nt":
        allowed.extend(_WINDOWS_ALLOWLIST)
    allowed.extend(_passthrough_names())

    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update(extra or {})
    return env


def dropped_names() -> list[str]:
    """Which host variables are being withheld. For logging and tests."""
    return sorted(set(os.environ) - set(build_env()))


def build_preexec(
    memory_mb: int = DEFAULT_MEMORY_MB,
    cpu_seconds: int = DEFAULT_CPU_SECONDS,
    file_size_mb: int = DEFAULT_FILE_SIZE_MB,
):
    """A `preexec_fn` applying rlimits, or None where unsupported.

    Returns None on Windows (no `resource` module) and wherever the limits
    can't be set — a missing cap is not a reason to refuse to run the
    extension at all.
    """
    if os.name == "nt":
        return None
    try:
        import resource
    except ImportError:
        return None

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        limits = (
            (resource.RLIMIT_AS, memory_mb * 1024 * 1024),
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_FSIZE, file_size_mb * 1024 * 1024),
        )
        for what, value in limits:
            try:
                soft, hard = resource.getrlimit(what)
                # Never raise an existing limit, and never exceed the hard cap
                # the OS already imposes.
                ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
                if soft != resource.RLIM_INFINITY:
                    ceiling = min(ceiling, soft)
                resource.setrlimit(what, (ceiling, hard))
            except (ValueError, OSError):
                continue

    return _apply


def describe() -> str:
    """One line for the logs, so the behaviour is discoverable."""
    if sandbox_disabled():
        return f"extension sandbox disabled via ${DISABLE_ENV}"
    dropped = len(dropped_names())
    extra = _passthrough_names()
    detail = f", {len(extra)} passed through" if extra else ""
    return f"extension env restricted ({dropped} host variables withheld{detail})"

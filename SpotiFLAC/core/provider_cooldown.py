"""core/provider_cooldown.py — a provider that asked for a break gets one.

tidal-py tries the Community (signed-desktop) gateway first on every track.
That gateway has enforced pauses — the SpotiFLAC desktop logic it comes from
blocks downloads for a while — and during one it answers 503 with how long
the pause lasts: "Please try again in about 51 minute(s)". The extension logs
that, falls back to public mirrors that time out, and fails; on the next
track it does all of it again. Roughly 25 seconds per track went to a
provider that had said, in words, when it would work again.

This reads that answer out of the extension's log and keeps the provider out
of the rotation until then. The provider is not removed: once the pause is
over it is tried first again, in the order the user configured. The pause is
kept in the cache directory, so a restart does not forget it and start the
25-second detour over.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from .atomic_io import write_json_atomic
from .paths import cache_path

logger = logging.getLogger(__name__)

_CACHE_FILE_NAME = "provider_cooldowns.json"

#: Module (and so logger) prefix of the Python `.sflx` extensions — see
#: extensions/python_provider._module_name.
_PLUGIN_PREFIX = "SpotiFLAC.extensions_plugins."

#: Logger prefix of the JS `.sflx` extensions. They have no module of their
#: own — the bridge relays their log lines from Node — so JSRuntime sends
#: them out under a child logger named for the extension, which is what
#: makes one extension's 503 tellable from another's.
_JS_LOG_PREFIX = "SpotiFLAC.extensions.runtime."

#: How a JS extension provider is named in PROVIDER_REGISTRY — see
#: extensions/provider.JSExtensionProvider.
_EXT_NAME_PREFIX = "ext:"

_UNAVAILABLE = re.compile(r"\b503\b")
_WAIT = re.compile(r"try again in (?:about )?(\d+)\s*(minute|min|hour|h)", re.I)

#: A pause longer than a day is read as a misparse, not as a promise.
_MAX_PAUSE_S = 24 * 3600

_lock = threading.Lock()


def _cache_file() -> Path:
    return cache_path(_CACHE_FILE_NAME)


def _load() -> dict[str, dict]:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict]) -> None:
    try:
        write_json_atomic(_cache_file(), data)
    except OSError:
        logger.debug("[cooldown] could not write %s", _cache_file(), exc_info=True)


def pause_seconds(message: str) -> int:
    """Seconds a 503 asks to wait before trying again, or 0 if it names none."""
    if not _UNAVAILABLE.search(message):
        return 0
    match = _WAIT.search(message)
    if not match:
        return 0
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * 3600 if unit.startswith("h") else amount * 60
    return min(seconds, _MAX_PAUSE_S)


def extension_key(logger_name: str) -> str:
    """The pause key of the extension that logger belongs to; "" for the host.

    Both kinds of extension are keyed by the name they are installed under:

      "SpotiFLAC.extensions_plugins.tidal_py"  → "tidal-py"   (Python)
      "SpotiFLAC.extensions.runtime.tidal-web" → "tidal-web"  (JS)

    A Python extension is a module, so its underscores are the ones a module
    name is obliged to have; a JS extension's logger is named for the
    extension directly and is left alone.
    """
    if logger_name.startswith(_PLUGIN_PREFIX):
        return logger_name[len(_PLUGIN_PREFIX) :].split(".", 1)[0].replace("_", "-")
    if logger_name.startswith(_JS_LOG_PREFIX):
        return logger_name[len(_JS_LOG_PREFIX) :].split(".", 1)[0]
    return ""


def provider_key(provider: Any) -> str:
    """The pause key of a provider object, or "" if it is not an extension.

    Two ways to the same key, because the two kinds of extension carry their
    identity in different places. PythonExtensionProvider returns the
    extension's own provider instance, so the class's module is the
    extension module whose logger reported the 503. A JS extension is a
    JSExtensionProvider whatever the extension is, so its module says
    nothing about which one — its registry name does: "ext:tidal-web" is the
    extension "tidal-web", the key its log lines already arrive under.

    Keying both from the installed name is what lets pause() and
    usable_providers() agree: a pause started from a JS extension's log has
    to name the provider object that gets skipped for it.
    """
    key = extension_key(type(provider).__module__)
    if key:
        return key
    name = str(getattr(provider, "name", "") or "")
    if name.startswith(_EXT_NAME_PREFIX):
        return name[len(_EXT_NAME_PREFIX) :]
    return ""


def pause(key: str, seconds: float, reason: str = "", now: float | None = None) -> None:
    """Keeps `key` out of the rotation for `seconds`. A shorter pause never
    cuts a longer one short."""
    if not key or seconds <= 0:
        return
    now = time.time() if now is None else now
    until = now + seconds
    with _lock:
        data = _load()
        try:
            current = float((data.get(key) or {}).get("until", 0))
        except (TypeError, ValueError):
            current = 0.0
        if current >= until:
            return
        data[key] = {"until": until, "reason": reason[:300]}
        _save(data)
    logger.warning(
        "[cooldown] %s paused for %d min (until %s): the service asked to try again later",
        key,
        max(1, round(seconds / 60)),
        time.strftime("%H:%M", time.localtime(until)),
    )


def remaining(key: str, now: float | None = None) -> float:
    """Seconds left on `key`'s pause; 0 when it has none."""
    if not key:
        return 0.0
    now = time.time() if now is None else now
    try:
        until = float((_load().get(key) or {}).get("until", 0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - now)


def usable_providers(providers: list) -> list:
    """`providers` without the ones on a pause, in the same order.

    All of them when every one is paused: a track with nothing left to try
    would fail without an attempt, and a pause is an estimate, not a ban.
    """
    active: list = []
    paused: list[tuple[Any, float]] = []
    for provider in providers:
        left = remaining(provider_key(provider))
        if left > 0:
            paused.append((provider, left))
        else:
            active.append(provider)
    if not paused or not active:
        return list(providers)
    for provider, left in paused:
        logger.info(
            "[cooldown] Skipping %s: paused for another %d min",
            provider_key(provider),
            max(1, round(left / 60)),
        )
    return active


class _PauseWatcher(logging.Handler):
    """Turns an extension's "503 … try again in about N minute(s)" into a pause."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        key = extension_key(record.name)
        if not key:
            return
        try:
            message = record.getMessage()
        except Exception:
            return
        seconds = pause_seconds(message)
        if seconds:
            pause(key, seconds, message)


_watcher = _PauseWatcher()


def watch_extension_logs() -> None:
    """Attaches the watcher to the root logger, again if it was removed.

    Cheap enough to call once per track, which it is: the GUI and --web
    reconfigure logging with basicConfig(force=True) when the log level
    changes, and that drops every handler on the root logger.
    """
    root = logging.getLogger()
    if _watcher not in root.handlers:
        root.addHandler(_watcher)

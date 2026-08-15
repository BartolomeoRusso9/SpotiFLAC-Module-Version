"""extensions/registry_config.py — Unified registry source management.

Collects extension-registry URLs from every place SpotiFLAC looks for them
(the ``SPOTIFLAC_REGISTRIES`` environment variable, ``.env``-style files, and
URLs the user has added from the GUI), and lets the user inspect and edit
that combined list from Settings.

Because we can't reach into the parent shell and truly ``unset`` a variable
the user exported with ``export SPOTIFLAC_REGISTRIES=...``, "removing" an
environment-sourced entry works like this:
  1. It is stripped from the in-memory ``os.environ`` value for the current
     process, so it stops being used immediately.
  2. It is written to a persisted "disabled" list, so it is filtered back
     out even if the same shell export is still active on the next launch.
  3. If the value also lives in a writable ``.env`` file, that file is
     rewritten to drop the URL, so the removal actually "sticks" at the
     source when possible.

Nothing here is hidden from the user: :func:`list_registries` always reports
where a URL came from and whether it is currently enabled.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_ENV_KEY = "SPOTIFLAC_REGISTRIES"

# Same set of files the extension manager falls back to.
ENV_FILES_TO_CHECK = (Path.cwd() / ".env", Path.home() / ".spotiflac_env")

# Where GUI-managed additions/removals are persisted.
CONFIG_FILE = Path.home() / ".spotiflac" / "registry_settings.json"

SOURCE_ENVIRONMENT = "environment"
SOURCE_ENV_FILE = "env_file"
SOURCE_CUSTOM = "custom"


@dataclass
class RegistrySource:
    url: str
    sources: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "sources": self.sources,
            "paths": self.paths,
            "enabled": self.enabled,
        }


# ─────────────────────────────────────────────────────────────
#  Persisted GUI config (custom URLs + disabled URLs)
# ─────────────────────────────────────────────────────────────


def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            data.setdefault("custom", [])
            data.setdefault("disabled", [])
            return data
    except Exception as e:
        logger.warning("[RegistryConfig] Unable to read %s: %s", CONFIG_FILE, e)
    return {"custom": [], "disabled": []}


def _save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[RegistryConfig] Unable to write %s: %s", CONFIG_FILE, e)


# ─────────────────────────────────────────────────────────────
#  Discovery helpers
# ─────────────────────────────────────────────────────────────


def _env_var_urls() -> list[str]:
    raw = os.environ.get(REGISTRY_ENV_KEY)
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def _env_file_urls() -> dict[str, list[Path]]:
    """Maps url -> list of .env-style files it was found in."""
    found: dict[str, list[Path]] = {}
    for p in ENV_FILES_TO_CHECK:
        try:
            if not p.exists():
                continue
            for ln in p.read_text(encoding="utf-8").splitlines():
                stripped = ln.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith(f"{REGISTRY_ENV_KEY}="):
                    _, val = stripped.split("=", 1)
                    for u in val.split(","):
                        u = u.strip()
                        if not u:
                            continue
                        found.setdefault(u, []).append(p)
        except Exception as e:
            logger.debug("[RegistryConfig] Unable to read %s: %s", p, e)
    return found


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────


def list_registries() -> list[dict]:
    """Returns every known registry URL with its origin(s) and enabled state."""
    cfg = _load_config()
    disabled = set(cfg.get("disabled", []))
    custom = cfg.get("custom", [])

    merged: dict[str, RegistrySource] = {}

    for u in _env_var_urls():
        entry = merged.setdefault(u, RegistrySource(url=u))
        if SOURCE_ENVIRONMENT not in entry.sources:
            entry.sources.append(SOURCE_ENVIRONMENT)

    for u, paths in _env_file_urls().items():
        entry = merged.setdefault(u, RegistrySource(url=u))
        if SOURCE_ENV_FILE not in entry.sources:
            entry.sources.append(SOURCE_ENV_FILE)
        for p in paths:
            sp = str(p)
            if sp not in entry.paths:
                entry.paths.append(sp)

    for u in custom:
        entry = merged.setdefault(u, RegistrySource(url=u))
        if SOURCE_CUSTOM not in entry.sources:
            entry.sources.append(SOURCE_CUSTOM)

    for entry in merged.values():
        entry.enabled = entry.url not in disabled

    # Stable order: enabled first, then alphabetically.
    ordered = sorted(merged.values(), key=lambda e: (not e.enabled, e.url.lower()))
    return [e.to_dict() for e in ordered]


def effective_urls() -> list[str]:
    """The final, deduplicated list of URLs actually used to fetch registries."""
    return [r["url"] for r in list_registries() if r["enabled"]]


def add_registry(url: str) -> list[dict]:
    """Adds a custom registry URL (from the GUI). Idempotent."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty registry URL")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Registry URL must start with http:// or https://")

    cfg = _load_config()
    custom = cfg.setdefault("custom", [])
    if url not in custom:
        custom.append(url)
    # Re-enable it in case it was previously disabled under this same URL.
    disabled = cfg.setdefault("disabled", [])
    if url in disabled:
        disabled.remove(url)
    _save_config(cfg)
    return list_registries()


def _strip_from_env_file(url: str, path: Path) -> None:
    """Best-effort removal of ``url`` from a SPOTIFLAC_REGISTRIES= line in an .env file."""
    try:
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        new_lines: list[str] = []
        for ln in lines:
            stripped = ln.strip()
            if stripped.startswith(f"{REGISTRY_ENV_KEY}="):
                prefix_len = len(ln) - len(ln.lstrip())
                indent = ln[:prefix_len]
                _, val = stripped.split("=", 1)
                urls = [u.strip() for u in val.split(",") if u.strip() and u.strip() != url]
                changed = True
                if urls:
                    new_lines.append(f"{indent}{REGISTRY_ENV_KEY}={','.join(urls)}")
                # else: drop the line entirely (no registries left)
                continue
            new_lines.append(ln)
        if changed:
            path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("[RegistryConfig] Unable to update %s: %s", path, e)


def remove_registry(url: str) -> list[dict]:
    """Removes a registry URL regardless of where it came from.

    - Custom (GUI-added) URLs are deleted outright.
    - Environment-variable URLs are stripped from the in-process
      ``os.environ`` value and recorded as disabled.
    - ``.env``-file URLs are removed from the file(s) they live in when
      writable, and recorded as disabled as a fallback.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty registry URL")

    cfg = _load_config()
    custom = cfg.setdefault("custom", [])
    disabled = set(cfg.setdefault("disabled", []))

    if url in custom:
        custom.remove(url)

    env_urls = _env_var_urls()
    if url in env_urls:
        remaining = [u for u in env_urls if u != url]
        if remaining:
            os.environ[REGISTRY_ENV_KEY] = ",".join(remaining)
        else:
            os.environ.pop(REGISTRY_ENV_KEY, None)
        disabled.add(url)

    file_hits = _env_file_urls()
    if url in file_hits:
        for p in file_hits[url]:
            _strip_from_env_file(url, p)
        disabled.add(url)

    cfg["disabled"] = sorted(disabled)
    _save_config(cfg)
    return list_registries()

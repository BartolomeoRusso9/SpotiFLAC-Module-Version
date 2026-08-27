"""extensions/directories.py — Registry *discovery*, one level above registries.

A registry (see `registry_config.py`) is a JSON file listing installable
extensions. A **directory** is a JSON file listing *registries* — pointers,
not packages: ``{"registries": [{"name", "url", "description", ...}, ...]}``.

Nothing is bundled here either, for the same reason nothing is bundled
anywhere else in this project (see the README's Extensions section): no
default directory URL ships with SpotiFLAC. You point it at a directory you
already trust — your own, a community one someone shared with you, whatever
— exactly the same opt-in shape as a registry itself:

- ``SPOTIFLAC_REGISTRY_DIRECTORIES`` env var (comma-separated URLs)
- ``--registry-directories URL`` CLI flag (repeatable), persisted so it only
  needs to be passed once
- the GUI's Discover screen (add/remove/list, same as registries)

Once you've added a directory, this module can fetch it and, for each
registry it lists, do a cheap reachability probe (see `probe_registry()`)
so the GUI can show a "reachable / unreachable, N extensions" badge next
to each one *before* you decide to add it as a registry of your own.
Probing is never automatic on its own —
it only runs when explicitly requested (e.g. the GUI's Discover tab, or
``probe_directories()`` called directly) — and adding a *registry* found
this way still goes through the normal, explicit ``registry_config.add_registry()``
flow; a directory listing something is never enough to install anything.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DIRECTORY_ENV_KEY = "SPOTIFLAC_REGISTRY_DIRECTORIES"
ENV_FILES_TO_CHECK = (Path.cwd() / ".env", Path.home() / ".spotiflac_env")
CONFIG_FILE = Path.home() / ".spotiflac" / "directory_settings.json"

_TIMEOUT_S = 10.0


@dataclass
class RegistryListing:
    """One registry entry as described by a directory (not yet added/trusted)."""

    name: str
    url: str
    description: str = ""
    maintainer: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "description": self.description,
            "maintainer": self.maintainer,
        }


@dataclass
class DirectoryHealth:
    """Result of probing one registry listed by a directory."""

    url: str
    reachable: bool
    extension_count: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "reachable": self.reachable,
            "extension_count": self.extension_count,
            "error": self.error,
        }


# ─────────────────────────────────────────────────────────────
#  Persisted GUI/CLI config (custom URLs + disabled URLs) — same shape as
#  registry_config.py, kept in its own file so directories and registries
#  can be added/removed independently of each other.
# ─────────────────────────────────────────────────────────────


def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            data.setdefault("custom", [])
            data.setdefault("disabled", [])
            return data
    except Exception as e:
        logger.warning("[Directories] Unable to read %s: %s", CONFIG_FILE, e)
    return {"custom": [], "disabled": []}


def _save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[Directories] Unable to write %s: %s", CONFIG_FILE, e)


def _env_var_urls() -> list[str]:
    raw = os.environ.get(DIRECTORY_ENV_KEY)
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def _env_file_urls() -> list[str]:
    found: list[str] = []
    for p in ENV_FILES_TO_CHECK:
        try:
            if not p.exists():
                continue
            for ln in p.read_text(encoding="utf-8").splitlines():
                stripped = ln.strip()
                if stripped.startswith("export "):
                    stripped = stripped[7:].strip()
                if stripped.startswith(f"{DIRECTORY_ENV_KEY}="):
                    _, val = stripped.split("=", 1)
                    val = val.strip().strip("'\"")
                    found.extend(u.strip() for u in val.split(",") if u.strip())
        except Exception as e:
            logger.debug("[Directories] Unable to read %s: %s", p, e)
    return found


def list_directory_urls() -> list[dict]:
    """Every known directory URL with its origin and enabled state — mirrors
    registry_config.list_registries()'s shape so the GUI can render both
    the same way.
    """
    cfg = _load_config()
    disabled = set(cfg.get("disabled", []))
    urls: dict[str, list[str]] = {}

    for u in _env_var_urls():
        urls.setdefault(u, []).append("environment")
    for u in _env_file_urls():
        urls.setdefault(u, []).append("env_file")
    for u in cfg.get("custom", []):
        urls.setdefault(u, []).append("custom")

    return [
        {"url": u, "sources": sources, "enabled": u not in disabled}
        for u, sources in sorted(urls.items(), key=lambda kv: kv[0].lower())
    ]


def effective_directory_urls() -> list[str]:
    return [d["url"] for d in list_directory_urls() if d["enabled"]]


def add_directory(url: str) -> list[dict]:
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty directory URL")
    if not url.startswith("https://"):
        raise ValueError("Directory URL must use https:// (http:// is not allowed)")

    cfg = _load_config()
    custom = cfg.setdefault("custom", [])
    if url not in custom:
        custom.append(url)
    disabled = cfg.setdefault("disabled", [])
    if url in disabled:
        disabled.remove(url)
    _save_config(cfg)
    return list_directory_urls()


def remove_directory(url: str) -> list[dict]:
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty directory URL")

    cfg = _load_config()
    custom = cfg.setdefault("custom", [])
    if url in custom:
        custom.remove(url)
    else:
        # Not a custom entry (env var/.env-sourced) — record as disabled,
        # same fallback registry_config.remove_registry() uses.
        disabled = set(cfg.setdefault("disabled", []))
        disabled.add(url)
        cfg["disabled"] = sorted(disabled)
    _save_config(cfg)
    return list_directory_urls()


# ─────────────────────────────────────────────────────────────
#  Fetching directories + probing what they list
# ─────────────────────────────────────────────────────────────


def fetch_directory(url: str) -> list[RegistryListing]:
    """Downloads and parses one directory JSON. Raises on network/parse
    failure — callers doing a bulk fetch across many directories should
    catch per-URL, same convention as ExtensionManager.fetch_registry().
    """
    resp = httpx.get(
        url,
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": "SpotiFLAC/DirectoryDiscovery"},
    )
    resp.raise_for_status()
    data = resp.json()

    listings = []
    for item in data.get("registries", []):
        if "name" not in item or "url" not in item:
            logger.warning(
                "[Directories] Skipping malformed listing (missing name/url) in %s",
                url,
            )
            continue
        listings.append(
            RegistryListing(
                name=item["name"],
                url=item["url"],
                description=item.get("description", ""),
                maintainer=item.get("maintainer", ""),
            ),
        )
    return listings


def fetch_all_directories(
    urls: list[str] | None = None,
) -> dict[str, list[RegistryListing]]:
    """Fetches every configured (or explicitly given) directory. Returns a
    dict of {directory_url: [listings]}; a directory that fails to fetch is
    simply omitted (logged, not raised) so one dead directory doesn't take
    down the rest of the Discover view.
    """
    targets = urls if urls is not None else effective_directory_urls()
    result: dict[str, list[RegistryListing]] = {}
    for url in targets:
        try:
            result[url] = fetch_directory(url)
        except Exception as e:
            logger.warning("[Directories] Failed to fetch directory %s: %s", url, e)
    return result


def probe_registry(url: str) -> DirectoryHealth:
    """Cheap reachability check for one registry URL a directory listed.

    Deliberately does *not* go through ExtensionManager.fetch_registry():
    that method treats a failed fetch as "zero entries" (logs a warning and
    moves on, by design — one dead registry shouldn't block startup), which
    is exactly the wrong behavior for a probe whose entire job is telling
    "unreachable" apart from "reachable but genuinely empty". This does its
    own minimal fetch instead, with no retry, so a real network/HTTP/parse
    failure surfaces as `reachable=False` rather than a silent 0.
    """
    try:
        resp = httpx.get(
            url,
            timeout=_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "SpotiFLAC/DirectoryDiscovery"},
        )
        resp.raise_for_status()
        data = resp.json()
        count = len(data.get("extensions", []))
        return DirectoryHealth(url=url, reachable=True, extension_count=count)
    except Exception as e:
        return DirectoryHealth(url=url, reachable=False, error=str(e)[:200])


def probe_directories(
    urls: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Fetches every configured directory and probes each registry it
    lists. Returns {directory_url: [{listing..., health...}, ...]} ready
    for the GUI to render as a badge per row.
    """
    directories = fetch_all_directories(urls)
    out: dict[str, list[dict]] = {}
    for directory_url, listings in directories.items():
        rows = []
        for listing in listings:
            health = probe_registry(listing.url)
            rows.append({**listing.to_dict(), "health": health.to_dict()})
        out[directory_url] = rows
    return out

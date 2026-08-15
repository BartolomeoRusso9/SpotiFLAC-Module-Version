"""extensions/manager.py — ExtensionManager.

Manages the lifecycle of locally installed extensions:
  - Fetch of remote registry
  - Installation / update from URL
  - Removal
  - Listing
  - Auto-setup of download providers on startup

Default directory: ~/.spotiflac/extensions/{name}/
  ├── index.js
  ├── manifest.json
  └── icon.jpg          (optional)
"""

from __future__ import annotations

import io
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import importlib.util
import sys
import traceback

logger = logging.getLogger(__name__)

# Registry configuration: no hardcoded URLs here — read from environment or .env
REGISTRY_URL = None
REGISTRY_ENV_KEY = "SPOTIFLAC_REGISTRIES"
ENV_FILES_TO_CHECK = (Path.cwd() / ".env", Path.home() / ".spotiflac_env")

DEFAULT_EXT_DIR = Path.home() / ".spotiflac" / "extensions"


# ─────────────────────────────────────────────────────────────
#  Models
# ─────────────────────────────────────────────────────────────


@dataclass
class RegistryEntry:
    id: str
    display_name: str
    version: str
    description: str
    download_url: str
    category: str = "unknown"
    tags: list[str] = field(default_factory=list)
    min_app_version: str = "0.0.0"
    icon_url: str | None = None
    updated_at: str = ""
    sha256: str | None = None


@dataclass
class InstalledExtension:
    name: str
    display_name: str
    version: str
    description: str
    ext_dir: Path
    manifest: dict = field(default_factory=dict)

    @property
    def index_js(self) -> Path:
        return self.ext_dir / "index.js"

    @property
    def entry_point(self) -> Path:
        # Support merged manifests that list per-runtime entry points under
        # `entryPoints`: {"python": "amazon.py", "javascript": "index.js"}
        eps = self.manifest.get("entryPoints")
        if isinstance(eps, dict):
            rt = self.runtime
            entry = eps.get(rt) or eps.get("javascript") or eps.get("python")
            if entry:
                return self.ext_dir / entry
        return self.ext_dir / self.manifest.get("entryPoint", "index.js")

    @property
    def runtime(self) -> str:
        runtimes = self.manifest.get("runtimes")
        if isinstance(runtimes, list) and runtimes:
            return "python" if "python" in runtimes else runtimes[0]
        return self.manifest.get("runtime", "javascript")

    @property
    def types(self) -> list[str]:
        return self.manifest.get("type", [])

    @property
    def is_download_provider(self) -> bool:
        return "download_provider" in self.types

    @property
    def is_metadata_provider(self) -> bool:
        return "metadata_provider" in self.types

    @property
    def url_patterns(self) -> list[str]:
        return self.manifest.get("urlHandler", {}).get("patterns", [])

    @property
    def settings_schema(self) -> list[dict]:
        return self.manifest.get("settings", [])

    def default_settings(self) -> dict:
        return {
            s["key"]: s.get("default", "") for s in self.settings_schema if "key" in s
        }


# ─────────────────────────────────────────────────────────────
#  ExtensionManager
# ─────────────────────────────────────────────────────────────


class ExtensionManager:
    """Central point for managing SpotiFLAC JS and Python extensions.

    Quick example:
        em = ExtensionManager(auto_install_downloads=True)
        # Automatically downloads or updates download providers on startup
    """

    _startup_registry_checks: set[str] = set()
    _startup_registry_checks_lock = threading.RLock()

    def __init__(
        self,
        ext_dir: str | Path | None = None,
        timeout: float = 20.0,
        auto_install_downloads: bool = True,  # Enabled by default
    ) -> None:
        self.ext_dir = Path(ext_dir) if ext_dir else DEFAULT_EXT_DIR
        self.timeout = timeout
        self.ext_dir.mkdir(parents=True, exist_ok=True)

        if auto_install_downloads:
            self.ensure_download_providers()

    # ── Auto Setup ───────────────────────────────────────────

    def ensure_download_providers(
        self, registry_url: str | list[str] | None = None
    ) -> None:
        """Checks the remote registry and automatically installs (or updates)
        all extensions classified as download providers AND utilities.

        The auto-setup is deduplicated per-process for the same registry
        configuration to avoid repeated startup fetches when multiple manager
        instances are created while the app is booting.
        """
        urls = self._registry_urls_from_env(registry_url)
        if not urls:
            logger.debug(
                "[ExtMgr] No registry URLs configured; skipping automatic startup bootstrap"
            )
            return

        registry_key = tuple(sorted(urls))

        with self.__class__._startup_registry_checks_lock:
            if registry_key in self.__class__._startup_registry_checks:
                logger.debug(
                    "[ExtMgr] Skipping duplicate registry bootstrap for %s",
                    registry_key,
                )
                return
            self.__class__._startup_registry_checks.add(registry_key)

        logger.info("[ExtMgr] Automatic check for download extensions on startup...")
        try:
            entries = self.fetch_registry(urls if urls else registry_url)
        except Exception as e:
            logger.warning("[ExtMgr] Unable to retrieve registry for auto-setup: %s", e)
            return

        # ORDINE CRITICO: Mettiamo prima le utility, così vengono scaricate prima dei provider
        entries.sort(
            key=lambda e: 0 if e.category in ("utility", "runtime_utility") else 1
        )

        for entry in entries:
            # FIX: Aggiungiamo 'utility' e 'runtime_utility' tra le categorie consentite
            is_target = (
                entry.category
                in {"download", "download_provider", "utility", "runtime_utility"}
                or "download" in entry.tags
                or "download_provider" in entry.tags
                or "utility" in entry.tags
            )

            if not is_target:
                continue

            existing = self.get_installed(entry.id)

            # If already installed and version matches, skip without downloading
            if existing and existing.version == entry.version:
                logger.debug(
                    "[ExtMgr] '%s' is already installed and updated (v%s)",
                    entry.id,
                    entry.version,
                )
                continue

            # Otherwise, installs or updates
            action = "Update" if existing else "Installation"
            logger.info(
                "[ExtMgr] %s of '%s' to version %s...",
                action,
                entry.id,
                entry.version,
            )
            try:
                self.install_from_url(entry.download_url, sha256=entry.sha256)
            except Exception as e:
                logger.exception(
                    "[ExtMgr] Error during %s of '%s': %s",
                    action.lower(),
                    entry.id,
                    e,
                )

    # ── Remote Registry ──────────────────────────────────────

    def _registry_urls_from_env(
        self, registry_url: str | list[str] | None
    ) -> list[str]:
        """Resolve registry URLs from parameter, environment variable, or .env file."""
        # If explicit provided, normalize to list
        if registry_url:
            if isinstance(registry_url, (list, tuple)):
                return [str(u) for u in registry_url]
            return [str(registry_url)]

        # Check environment variable
        env_val = os.environ.get(REGISTRY_ENV_KEY)
        if env_val:
            return [u.strip() for u in env_val.split(",") if u.strip()]

        # Try to read .env-like files from common locations
        for p in ENV_FILES_TO_CHECK:
            try:
                if p.exists():
                    for ln in p.read_text(encoding="utf-8").splitlines():
                        ln = ln.strip()
                        if not ln or ln.startswith("#"):
                            continue
                        if ln.startswith(f"{REGISTRY_ENV_KEY}="):
                            _, val = ln.split("=", 1)
                            return [u.strip() for u in val.split(",") if u.strip()]
            except Exception:
                continue
        # No registries configured
        return []

    def fetch_registry(self, url: str | list[str] | None = None) -> list[RegistryEntry]:
        """Downloads and parses one or more remote registry.json files.

        Accepts a single URL, a list of URLs, or None (then read from env/.env).
        Returns aggregated list of RegistryEntry objects from all reachable registries.
        """
        urls = self._registry_urls_from_env(url)
        logger.debug("[ExtMgr] Fetching registries: %s", urls)
        if not urls:
            raise RuntimeError(
                "No registry URLs configured; set SPOTIFLAC_REGISTRIES in .env or environment"
            )

        entries: list[RegistryEntry] = []
        for u in urls:
            try:
                r = httpx.get(u, timeout=self.timeout, follow_redirects=True)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.warning("[ExtMgr] Failed to download registry %s: %s", u, e)
                continue

            for item in data.get("extensions", []):
                entries.append(
                    RegistryEntry(
                        id=item["id"],
                        display_name=item.get("display_name", item["id"]),
                        version=item.get("version", "0.0.0"),
                        description=item.get("description", ""),
                        download_url=item["download_url"],
                        category=item.get("category", "unknown"),
                        tags=item.get("tags", []),
                        min_app_version=item.get("min_app_version", "0.0.0"),
                        icon_url=item.get("icon_url"),
                        updated_at=item.get("updated_at", ""),
                        sha256=item.get("sha256"),
                    ),
                )
        return entries

    # ── Installation ────────────────────────────────────────

    def install(
        self,
        ext_id: str,
        registry_url: str | list[str] | None = None,
        settings: dict | None = None,
    ) -> InstalledExtension:
        """Installs an extension by ID from the official registry.
        If already installed, updates only if the remote version is newer.
        """
        entries = self.fetch_registry(registry_url)
        entry = next((e for e in entries if e.id == ext_id), None)
        if entry is None:
            available = ", ".join(e.id for e in entries)
            msg = f"Extension '{ext_id}' not found in registry. Available: {available}"
            raise ValueError(
                msg,
            )

        # Check if already installed and up-to-date
        existing = self.get_installed(ext_id)
        if existing and existing.version == entry.version:
            logger.info("[ExtMgr] '%s' already up-to-date (v%s)", ext_id, entry.version)
            return existing

        return self.install_from_url(
            entry.download_url,
            settings=settings,
            sha256=entry.sha256,
        )

    def install_from_url(
        self,
        url: str,
        settings: dict | None = None,
        sha256: str | None = None,
        runtime_hint: str | None = None,
    ) -> InstalledExtension:
        """Downloads a .spotiflac-ext or .sflx file (ZIP) from `url` and installs it.
        The extension name is read from `manifest.json` inside the ZIP.
        """
        logger.debug("[ExtMgr] Downloading extension from %s", url)
        # Parse fragment hints like: https://.../file.spotiflac-ext#tags=python
        parsed_runtime = None
        if "#" in url:
            url, frag = url.split("#", 1)
            # frag format: tags=python,download or runtime=python
            for part in frag.split("&"):
                if part.startswith("tags="):
                    tags = part.split("=", 1)[1]
                    if "python" in tags.split(","):
                        parsed_runtime = "python"
                if part.startswith("runtime="):
                    parsed_runtime = part.split("=", 1)[1]
        # runtime_hint explicit parameter overrides fragment
        if runtime_hint:
            parsed_runtime = runtime_hint
        try:
            r = httpx.get(url, timeout=self.timeout * 3, follow_redirects=True)
            r.raise_for_status()
            raw = r.content
        except Exception as e:
            msg = f"Error downloading extension: {e}"
            raise RuntimeError(msg) from e

        return self._install_from_bytes(
            raw, settings=settings, sha256=sha256, runtime_hint=parsed_runtime
        )

    def install_from_file(
        self,
        path: str | Path,
        settings: dict | None = None,
    ) -> InstalledExtension:
        """Installs from a local file (ZIP)."""
        raw = Path(path).read_bytes()
        return self._install_from_bytes(raw, settings=settings)

    def _install_from_bytes(
        self,
        raw: bytes,
        settings: dict | None = None,
        sha256: str | None = None,
        runtime_hint: str | None = None,
    ) -> InstalledExtension:
        # Validate checksum if provided by the registry.
        if sha256:
            actual = hashlib.sha256(raw).hexdigest().lower()
            expected = sha256.lower()
            if actual != expected:
                allow = os.environ.get("SPOTIFLAC_ALLOW_CHECKSUM_MISMATCH", "").lower()
                if allow in ("1", "true", "yes", "y"):
                    logger.warning(
                        "[ExtMgr] Checksum mismatch for extension (expected=%s actual=%s) — proceeding because SPOTIFLAC_ALLOW_CHECKSUM_MISMATCH is set",
                        expected,
                        actual,
                    )
                else:
                    raise ValueError("Extension checksum does not match the registry")
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            msg = f"File is not a valid extension archive (ZIP): {e}"
            raise ValueError(msg) from e

        names = zf.namelist()
        python_modules = [
            name for name in names if name.endswith(".py") and "/" not in name
        ]
        manifest = None
        if "manifest.json" in names:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except Exception:
                manifest = None

        # Legacy python: missing manifest but single top-level .py
        is_legacy_python = manifest is None and len(python_modules) == 1

        if not is_legacy_python:
            if "manifest.json" not in names:
                msg = f"The archive must contain manifest.json. " f"Found: {names}"
                raise ValueError(msg)
            if "index.js" not in names and not (
                manifest and manifest.get("runtime") == "python"
            ):
                msg = (
                    f"The archive must contain manifest.json and index.js (unless runtime is python). "
                    f"Found: {names}"
                )
                raise ValueError(msg)

        if is_legacy_python:
            module = Path(python_modules[0]).stem
            name = module.removesuffix("_native").replace("_", "-")
            utility = module in {
                "solver",
                "signed_session_mobile",
                "signed_session_desktop",
                "signed_session_mono",
            }
            manifest = {
                "name": name,
                "displayName": name,
                "version": "0.0.0+legacy",
                "runtime": "python",
                "entryPoint": python_modules[0],
                "type": ["runtime_utility" if utility else "download_provider"],
                "legacyPackage": True,
            }
        else:
            manifest = json.loads(zf.read("manifest.json"))

        if runtime_hint == "python" and python_modules:
            manifest = dict(manifest)
            manifest["runtime"] = "python"
            if "entryPoint" not in manifest or not manifest.get("entryPoint"):
                manifest["entryPoint"] = python_modules[0]

        if "runtime" not in manifest or not manifest.get("runtime"):
            inferred = None
            if "index.js" in names:
                inferred = "javascript"
            elif python_modules:
                inferred = "python"
                manifest = dict(manifest)
                manifest["entryPoint"] = manifest.get("entryPoint") or python_modules[0]
            if inferred:
                manifest = dict(manifest)
                manifest["runtime"] = inferred

        ext_name = manifest.get("name")
        if not ext_name:
            msg = "manifest.json must have the 'name' field."
            raise ValueError(msg)

        target = self.ext_dir / ext_name

        if any(
            Path(member).is_absolute() or ".." in Path(member).parts for member in names
        ):
            raise ValueError("Extension archive contains an unsafe path")

        previous_settings = target / "settings.json"
        saved_settings = (
            previous_settings.read_bytes() if previous_settings.exists() else None
        )
        staging = Path(tempfile.mkdtemp(prefix=f".{ext_name}-", dir=self.ext_dir))

        try:
            for member in names:
                destination = staging / member
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(zf.read(member))

            if is_legacy_python:
                (staging / "manifest.json").write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8"
                )
            if saved_settings and not settings:
                (staging / "settings.json").write_bytes(saved_settings)
            if settings:
                (staging / "settings.json").write_text(
                    json.dumps(settings, indent=2), encoding="utf-8"
                )

            # Standard Overwrite (removes old version entirely, replaces with new)
            backup = target.with_suffix(".previous")
            if backup.exists():
                shutil.rmtree(backup)
            if target.exists():
                os.replace(target, backup)

            os.replace(staging, target)

            if backup.exists():
                shutil.rmtree(backup)

        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        logger.info(
            "[ExtMgr] Success: '%s' v%s installed.",
            ext_name,
            manifest.get("version"),
        )
        return self._load_installed(target)

    # ── Removal ────────────────────────────────────────────

    def uninstall(self, ext_id: str) -> bool:
        """Removes an installed extension. Returns True if found and removed."""
        target = self.ext_dir / ext_id
        if target.exists():
            shutil.rmtree(target)
            logger.info("[ExtMgr] Uninstalled '%s'", ext_id)
            return True
        logger.warning("[ExtMgr] '%s' not installed", ext_id)
        return False

    # ── Listing ──────────────────────────────────────────────

    def list_installed(self) -> list[InstalledExtension]:
        """Returns all installed extensions."""
        result = []
        for d in sorted(self.ext_dir.iterdir()):
            if d.is_dir() and (d / "manifest.json").exists():
                try:
                    result.append(self._load_installed(d))
                except Exception as e:
                    logger.warning("[ExtMgr] Skip '%s': %s", d.name, e)
        return result

    def get_installed(self, ext_id: str) -> InstalledExtension | None:
        """Returns an installed extension by ID, or None if not found."""
        target = self.ext_dir / ext_id
        if target.exists() and (target / "manifest.json").exists():
            try:
                return self._load_installed(target)
            except Exception:
                return None
        return None

    def load_settings(self, ext_id: str) -> dict:
        """Loads saved settings for an extension (merge with defaults)."""
        ext = self.get_installed(ext_id)
        if not ext:
            return {}
        defaults = ext.default_settings()
        settings_path = ext.ext_dir / "settings.json"
        if settings_path.exists():
            try:
                saved = json.loads(settings_path.read_text(encoding="utf-8"))
                defaults.update(saved)
            except Exception:
                pass
        return defaults

    def install_from_links_file(
        self, path: str | Path | None = None
    ) -> list[InstalledExtension]:
        """Reads a simple env-like file containing extension download URLs."""
        candidates: list[InstalledExtension] = []
        path_to_try = None
        if path:
            path_to_try = Path(path)
        else:
            env_path = os.environ.get("SPOTIFLAC_EXT_LINKS")
            if env_path:
                path_to_try = Path(env_path)
            else:
                default = self.ext_dir / "extensions_links.env"
                if default.exists():
                    path_to_try = default

        if not path_to_try or not path_to_try.exists():
            raise FileNotFoundError("Extensions links file not found")

        for line in path_to_try.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            url = line
            runtime_hint = None
            if " " in line:
                parts = line.split()
                url = parts[0]
                for p in parts[1:]:
                    if p.startswith("tags="):
                        tags = p.split("=", 1)[1]
                        if "python" in tags.split(","):
                            runtime_hint = "python"
                    if p.startswith("runtime="):
                        runtime_hint = p.split("=", 1)[1]
            try:
                inst = self.install_from_url(url, runtime_hint=runtime_hint)
                candidates.append(inst)
            except Exception:
                logger.exception("[ExtMgr] Failed installing from link: %s", url)
        return candidates

    def save_settings(self, ext_id: str, settings: dict) -> None:
        """Saves custom settings for an extension."""
        ext = self.get_installed(ext_id)
        if not ext:
            msg = f"Extension '{ext_id}' not installed."
            raise ValueError(msg)
        (ext.ext_dir / "settings.json").write_text(
            json.dumps(settings, indent=2),
            encoding="utf-8",
        )

    # ── URL Resolution ──────────────────────────────────────

    def find_extension_for_url(self, url: str) -> InstalledExtension | None:
        """Returns the first installed extension whose urlHandler
        matches the provided URL.
        """
        url_lower = url.lower()
        for ext in self.list_installed():
            for pattern in ext.url_patterns:
                if pattern.lower() in url_lower:
                    return ext
        return None

    # ── Helpers ─────────────────────────────────────────────

    def _load_installed(self, ext_dir: Path) -> InstalledExtension:
        manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
        return InstalledExtension(
            name=manifest.get("name", ext_dir.name),
            display_name=manifest.get("displayName", ext_dir.name),
            version=manifest.get("version", "0.0.0"),
            description=manifest.get("description", ""),
            ext_dir=ext_dir,
            manifest=manifest,
        )

    # ── Batch update ──────────────────────────────────────

    def update_all(self, registry_url: str | list[str] | None = None) -> dict[str, str]:
        """Updates all installed extensions that have a newer version
        in the registry.
        """
        installed = {e.name: e for e in self.list_installed()}
        if not installed:
            return {}

        entries = {e.id: e for e in self.fetch_registry(registry_url)}
        status: dict[str, str] = {}

        for name, ext in installed.items():
            if name not in entries:
                status[name] = "not_in_registry"
                continue
            remote = entries[name]
            if remote.version != ext.version:
                try:
                    self.install_from_url(remote.download_url, sha256=remote.sha256)
                    status[name] = f"updated → {remote.version}"
                except Exception as e:
                    status[name] = f"error: {e}"
            else:
                status[name] = "already_up_to_date"

        return status

    def preload_python_modules(self) -> None:
        """Pre-loads all installed Python extension entry points into sys.modules.

        This helps resolve intra-extension imports that expect other extension
        modules (e.g. `from .tidal import ...`) to be available under the
        canonical `SpotiFLAC.extensions_plugins.<name>` package name.
        """
        pkg_name = "SpotiFLAC.extensions_plugins"
        python_paths: list[str] = []
        python_exts = [e for e in self.list_installed() if e.runtime == "python"]
        for ext in python_exts:
            # We now rely directly on the parent directory of the entry point,
            # as subdirectories (like python/) are no longer generated.
            python_paths.append(str(ext.entry_point.parent))

        import types

        if pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = python_paths[:]
            sys.modules[pkg_name] = pkg
        else:
            pkg = sys.modules[pkg_name]
            existing = list(getattr(pkg, "__path__", []))
            for p in python_paths:
                if p not in existing:
                    existing.append(p)
            pkg.__path__ = existing

        for ext in python_exts:
            module_name = f"{pkg_name}.{ext.name.replace('-', '_')}"
            if module_name in sys.modules:
                continue
            mod = types.ModuleType(module_name)
            mod.__package__ = pkg_name
            try:
                mod.__file__ = str(ext.entry_point)
            except Exception:
                pass
            sys.modules[module_name] = mod

        for ext in python_exts:
            module_name = f"{pkg_name}.{ext.name.replace('-', '_')}"
            try:
                spec = importlib.util.spec_from_file_location(
                    module_name, ext.entry_point
                )
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                module.__package__ = pkg_name
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise
            except Exception:
                logger.debug(
                    "[ExtMgr] Failed preloading %s: %s",
                    ext.name,
                    traceback.format_exc(),
                )

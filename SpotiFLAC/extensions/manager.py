"""
extensions/manager.py — ExtensionManager

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
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import httpx

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://raw.githubusercontent.com/zarzet/SpotiFLAC-Extension/main/registry.json"

DEFAULT_EXT_DIR = Path.home() / ".spotiflac" / "extensions"


# ─────────────────────────────────────────────────────────────
#  Modelli
# ─────────────────────────────────────────────────────────────

@dataclass
class RegistryEntry:
    id:              str
    display_name:    str
    version:         str
    description:     str
    download_url:    str
    category:        str             = "unknown"
    tags:            list[str]       = field(default_factory=list)
    min_app_version: str             = "0.0.0"
    icon_url:        str | None      = None
    updated_at:      str             = ""


@dataclass
class InstalledExtension:
    name:          str
    display_name:  str
    version:       str
    description:   str
    ext_dir:       Path
    manifest:      dict             = field(default_factory=dict)

    @property
    def index_js(self) -> Path:
        return self.ext_dir / "index.js"

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
        return (
            self.manifest
            .get("urlHandler", {})
            .get("patterns", [])
        )

    @property
    def settings_schema(self) -> list[dict]:
        return self.manifest.get("settings", [])

    def default_settings(self) -> dict:
        return {
            s["key"]: s.get("default", "")
            for s in self.settings_schema
            if "key" in s
        }


# ─────────────────────────────────────────────────────────────
#  ExtensionManager
# ─────────────────────────────────────────────────────────────

class ExtensionManager:
    """
    Central point for managing SpotiFLAC JS extensions.

    Quick example:
        em = ExtensionManager(auto_install_downloads=True)
        # Automaticamente scarica o aggiorna i provider di download all'avvio
    """

    def __init__(
        self,
        ext_dir: str | Path | None = None,
        timeout: float = 20.0,
        auto_install_downloads: bool = True,  # Attivato di default
    ) -> None:
        self.ext_dir = Path(ext_dir) if ext_dir else DEFAULT_EXT_DIR
        self.timeout = timeout
        self.ext_dir.mkdir(parents=True, exist_ok=True)

        if auto_install_downloads:
            self.ensure_download_providers()

    # ── Auto Setup ───────────────────────────────────────────

    def ensure_download_providers(self, registry_url: str = REGISTRY_URL) -> None:
        """
        Verifica il registry remoto e installa (o aggiorna) automaticamente
        tutte le estensioni classificate come download provider.
        """
        logger.info("[ExtMgr] Controllo automatico estensioni di download all'avvio...")
        try:
            entries = self.fetch_registry(registry_url)
        except Exception as e:
            logger.warning("[ExtMgr] Impossibile recuperare il registry per l'auto-setup: %s", e)
            return

        for entry in entries:
            # Identifica se l'estensione è un download provider tramite categoria o tag
            is_download = (
                entry.category == "download_provider" 
                or "download" in entry.tags 
                or "download_provider" in entry.tags
            )
            
            if not is_download:
                continue

            existing = self.get_installed(entry.id)
            
            # Se è già installata e la versione corrisponde, salta senza fare download
            if existing and existing.version == entry.version:
                logger.debug("[ExtMgr] '%s' è già installata e aggiornata (v%s)", entry.id, entry.version)
                continue

            # Altrimenti, installa o aggiorna
            action = "Aggiornamento" if existing else "Installazione"
            logger.info("[ExtMgr] %s di '%s' alla versione %s...", action, entry.id, entry.version)
            try:
                self.install_from_url(entry.download_url)
            except Exception as e:
                logger.error("[ExtMgr] Errore durante l'%s di '%s': %s", action.lower(), entry.id, e)

    # ── Remote Registry ──────────────────────────────────────

    def fetch_registry(self, url: str = REGISTRY_URL) -> list[RegistryEntry]:
        """Downloads and parses the remote registry.json."""
        logger.debug("[ExtMgr] Fetching registry from %s", url)
        try:
            r = httpx.get(url, timeout=self.timeout, follow_redirects=True)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"Failed to download registry: {e}") from e

        entries = []
        for item in data.get("extensions", []):
            entries.append(RegistryEntry(
                id              = item["id"],
                display_name    = item.get("display_name", item["id"]),
                version         = item.get("version", "0.0.0"),
                description     = item.get("description", ""),
                download_url    = item["download_url"],
                category        = item.get("category", "unknown"),
                tags            = item.get("tags", []),
                min_app_version = item.get("min_app_version", "0.0.0"),
                icon_url        = item.get("icon_url"),
                updated_at      = item.get("updated_at", ""),
            ))
        return entries

    # ── Installation ────────────────────────────────────────

    def install(
        self,
        ext_id: str,
        registry_url: str = REGISTRY_URL,
        settings: dict | None = None,
    ) -> InstalledExtension:
        """
        Installs an extension by ID from the official registry.
        If already installed, updates only if the remote version is newer.
        """
        entries = self.fetch_registry(registry_url)
        entry   = next((e for e in entries if e.id == ext_id), None)
        if entry is None:
            available = ", ".join(e.id for e in entries)
            raise ValueError(f"Extension '{ext_id}' not found in registry. Available: {available}")

        # Check if already installed and up-to-date
        existing = self.get_installed(ext_id)
        if existing and existing.version == entry.version:
            logger.info("[ExtMgr] '%s' already up-to-date (v%s)", ext_id, entry.version)
            return existing

        return self.install_from_url(entry.download_url, settings=settings)

    def install_from_url(
        self,
        url: str,
        settings: dict | None = None,
    ) -> InstalledExtension:
        """
        Downloads a .spotiflac-ext file (ZIP) from `url` and installs it.
        The extension name is read from `manifest.json` inside the ZIP.
        """
        logger.debug("[ExtMgr] Downloading extension from %s", url)
        try:
            r = httpx.get(url, timeout=self.timeout * 3, follow_redirects=True)
            r.raise_for_status()
            raw = r.content
        except Exception as e:
            raise RuntimeError(f"Error downloading extension: {e}") from e

        return self._install_from_bytes(raw, settings=settings)

    def install_from_file(
        self,
        path: str | Path,
        settings: dict | None = None,
    ) -> InstalledExtension:
        """Installs from a local .spotiflac-ext file (ZIP)."""
        raw = Path(path).read_bytes()
        return self._install_from_bytes(raw, settings=settings)

    def _install_from_bytes(
        self,
        raw: bytes,
        settings: dict | None = None,
    ) -> InstalledExtension:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise ValueError(f"File is not a valid .spotiflac-ext (ZIP): {e}") from e

        names = zf.namelist()
        if "manifest.json" not in names or "index.js" not in names:
            raise ValueError(
                f"The .spotiflac-ext must contain manifest.json and index.js. "
                f"Found: {names}"
            )

        manifest = json.loads(zf.read("manifest.json"))
        ext_name = manifest.get("name")
        if not ext_name:
            raise ValueError("manifest.json must have the 'name' field.")

        target = self.ext_dir / ext_name
        target.mkdir(parents=True, exist_ok=True)

        # Extract all files
        for member in names:
            data = zf.read(member)
            (target / member).write_bytes(data)

        # Save custom settings if provided
        if settings:
            (target / "settings.json").write_text(
                json.dumps(settings, indent=2), encoding="utf-8"
            )

        logger.info("[ExtMgr] Success: '%s' v%s installed.", ext_name, manifest.get("version"))
        return self._load_installed(target)

    # ── Removal ────────────────────────────────────────────

    def uninstall(self, ext_id: str) -> bool:
        """Removes an installed extension. Returns True if found and removed."""
        import shutil
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

    def save_settings(self, ext_id: str, settings: dict) -> None:
        """Saves custom settings for an extension."""
        ext = self.get_installed(ext_id)
        if not ext:
            raise ValueError(f"Extension '{ext_id}' not installed.")
        (ext.ext_dir / "settings.json").write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )

    # ── URL Resolution ──────────────────────────────────────

    def find_extension_for_url(self, url: str) -> InstalledExtension | None:
        """
        Returns the first installed extension whose urlHandler
        matches the provided URL.
        """
        url_lower = url.lower()
        for ext in self.list_installed():
            for pattern in ext.url_patterns:
                if pattern.lower() in url_lower:
                    return ext
        return None

    # ── Helpers ──────────────────────────────────────────────

    def _load_installed(self, ext_dir: Path) -> InstalledExtension:
        manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
        return InstalledExtension(
            name         = manifest.get("name", ext_dir.name),
            display_name = manifest.get("displayName", ext_dir.name),
            version      = manifest.get("version", "0.0.0"),
            description  = manifest.get("description", ""),
            ext_dir      = ext_dir,
            manifest     = manifest,
        )

    # ── Batch update ──────────────────────────────────────

    def update_all(self, registry_url: str = REGISTRY_URL) -> dict[str, str]:
        """
        Updates all installed extensions that have a newer version
        in the registry.
        Returns dict {ext_id: 'updated'|'already_up_to_date'|'not_in_registry'}.
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
                    self.install_from_url(remote.download_url)
                    status[name] = f"updated → {remote.version}"
                except Exception as e:
                    status[name] = f"error: {e}"
            else:
                status[name] = "already_up_to_date"

        return status
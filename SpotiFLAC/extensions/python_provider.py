"""Adapter for legacy Python `.sflx` packages."""

from __future__ import annotations

import importlib.util
import logging
import sys
from typing import Any

from SpotiFLAC.core.base import BaseProvider

from .manager import ExtensionManager, InstalledExtension

logger = logging.getLogger(__name__)

_UTILITY_ORDER = (
    "solver",
    "signed-session-mobile",
    "signed-session-desktop",
    "signed-session-mono",
)


def _module_name(ext: InstalledExtension) -> str:
    return f"SpotiFLAC.extensions_plugins.{ext.name.replace('-', '_')}"


def _load(ext: InstalledExtension, name: str | None = None):
    module_name = name or _module_name(ext)
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, ext.entry_point)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Python extension: {ext.entry_point}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(module_name, None)
        logger.error(f"[Utilities] Error executing module {module_name}: {e}")
        raise
    return module


def load_python_utilities(manager: ExtensionManager) -> None:
    """Install utility modules under the canonical names used by providers."""
    installed = manager.list_installed()
    for base_name in _UTILITY_ORDER:
        ext = next(
            (
                c
                for c in installed
                if c.runtime == "python"
                and base_name.replace("-", "") in c.name.replace("-", "").lower()
            ),
            None,
        )
        if not ext:
            logger.warning(f"[Utilities] Utility '{base_name}' non trovata sul disco.")
            continue

        canonical = f"SpotiFLAC.core.{base_name.replace('-', '_')}"

        # Prevent extensions from shadowing first-party SpotiFLAC.core modules
        if canonical in sys.modules:
            existing_module = sys.modules[canonical]
            # Check if it's a first-party module (already loaded from SpotiFLAC/core/)
            module_file = getattr(existing_module, "__file__", None)
            if module_file and "SpotiFLAC/core/" in module_file.replace("\\", "/"):
                logger.error(
                    f"[Utilities] Refusing to load extension utility '{base_name}': "
                    f"name collision with first-party module {canonical}"
                )
                continue

        try:
            _load(ext, canonical)
            logger.info(f"[Utilities] Caricata con successo l'utility {canonical}")
        except Exception as e:
            logger.warning(
                f"[Utilities] Impossibile caricare l'utility {base_name}: {e}"
            )


class PythonExtensionProvider(BaseProvider):
    """Loads a trusted Python provider extension and delegates BaseProvider calls."""

    def __new__(cls, ext_id: str, *, ext_dir: str | None = None, **kwargs: Any):
        manager = ExtensionManager(ext_dir=ext_dir, auto_install_downloads=False)
        load_python_utilities(manager)
        try:
            manager.preload_python_modules()
        except Exception:
            pass

        base_name = (
            ext_id.replace("ext:", "").replace("-web", "").replace("-py", "").lower()
        )
        ext_name = manager.find_python_extension(base_name)
        if ext_name is None:
            raise ValueError(f"Python extension for '{ext_id}' is not installed")

        ext = manager.get_installed(ext_name)
        if ext is None:
            raise ValueError(f"Python extension for '{ext_id}' is not installed")

        module = _load(ext)
        candidates = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, BaseProvider)
            and value is not BaseProvider
        ]
        if len(candidates) != 1:
            raise TypeError(
                f"Extension '{ext_id}' must expose exactly one BaseProvider subclass"
            )

        return candidates[0](**kwargs)

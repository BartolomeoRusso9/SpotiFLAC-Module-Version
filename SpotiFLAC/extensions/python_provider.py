"""Adapter for legacy Python `.sflx` packages."""

from __future__ import annotations

import importlib.util
import logging
import sys
from typing import Any

from SpotiFLAC.core.base import BaseProvider

from .manager import ExtensionManager, InstalledExtension

logger = logging.getLogger(__name__)


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

class PythonExtensionProvider(BaseProvider):
    """Loads a trusted Python provider extension and delegates BaseProvider calls."""

    def __new__(cls, ext_id: str, *, ext_dir: str | None = None, **kwargs: Any):
        manager = ExtensionManager(ext_dir=ext_dir, auto_install_downloads=False)
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

"""Adapter for legacy Python `.sflx` packages.

The package format is deliberately explicit: Python extensions execute in the
application interpreter and therefore are for trusted registries only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from SpotiFLAC.core.base import BaseProvider

from .manager import ExtensionManager, InstalledExtension

_UTILITY_ORDER = ("solver", "signed-session-mobile", "signed-session-desktop", "signed-session-mono")


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
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_python_utilities(manager: ExtensionManager) -> None:
    """Install utility modules under the canonical names used by providers."""
    for ext_id in _UTILITY_ORDER:
        ext = manager.get_installed(ext_id)
        if not ext or ext.runtime != "python":
            continue
        canonical = f"SpotiFLAC.core.{Path(ext.entry_point).stem}"
        _load(ext, canonical)


class PythonExtensionProvider(BaseProvider):
    """Loads a trusted Python provider extension and delegates BaseProvider calls."""

    def __new__(cls, ext_id: str, *, ext_dir: str | None = None, **kwargs: Any):
        manager = ExtensionManager(ext_dir=ext_dir, auto_install_downloads=False)
        load_python_utilities(manager)
        ext = manager.get_installed(ext_id)
        if ext is None or ext.runtime != "python":
            raise ValueError(f"Python extension '{ext_id}' is not installed")
        module = _load(ext)
        candidates = [value for value in vars(module).values() if isinstance(value, type) and issubclass(value, BaseProvider) and value is not BaseProvider]
        if len(candidates) != 1:
            raise TypeError(f"Extension '{ext_id}' must expose exactly one BaseProvider subclass")
        return candidates[0](**kwargs)

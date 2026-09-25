"""Backward-compatible downloader module.

The implementation is owned by ``SpotiFLAC.application.download_engine``.
This module remains as a compatibility facade for existing imports and legacy
monkeypatch targets such as ``SpotiFLAC.downloader.SpotiflacDownloader``.

class LegacyDownloadWorker is intentionally preserved as a compatibility name;
its implementation now lives in the application engine.
"""

from __future__ import annotations

import sys
import types

from .application import download_engine as _engine


class _DownloaderCompatModule(types.ModuleType):
    """Proxy reads and writes to the application-owned download engine."""

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(_engine, name)

    def __setattr__(self, name: str, value) -> None:  # type: ignore[no-untyped-def]
        setattr(_engine, name, value)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(dir(_engine)))


sys.modules[__name__].__class__ = _DownloaderCompatModule

__all__ = [name for name in dir(_engine) if not name.startswith("_")]

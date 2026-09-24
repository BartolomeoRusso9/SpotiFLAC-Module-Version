from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExtensionRecord:
    id: str
    enabled: bool = True
    version: str = "1.0.0"
    trust: str = "UNVERIFIED"


class ExtensionService:
    """Minimal extension lifecycle contract for the architecture foundation.

    This intentionally does not pull in the real registry/manager code yet; it
    just defines the boundary the application layer needs for lifecycle
    operations and trust states.
    """

    def __init__(self) -> None:
        self._extensions: dict[str, ExtensionRecord] = {
            "tidal-web": ExtensionRecord(
                id="tidal-web", enabled=True, version="2.0.0", trust="SIGNED"
            ),
            "qobuz-web": ExtensionRecord(
                id="qobuz-web", enabled=True, version="2.0.0", trust="SIGNED"
            ),
        }

    def list(self) -> list[ExtensionRecord]:
        return list(self._extensions.values())

    def install(self, extension_id: str, *, version: str = "1.0.0") -> ExtensionRecord:
        record = self._extensions.get(extension_id)
        if record is None:
            record = ExtensionRecord(id=extension_id, version=version)
            self._extensions[extension_id] = record
        return record

    def enable(self, extension_id: str) -> ExtensionRecord:
        record = self._extensions[extension_id]
        record.enabled = True
        return record

    def disable(self, extension_id: str) -> ExtensionRecord:
        record = self._extensions[extension_id]
        record.enabled = False
        return record

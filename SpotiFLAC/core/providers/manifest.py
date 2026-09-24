from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtensionManifest:
    """Stable, runtime-independent provider capability contract."""

    id: str
    version: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    qualities: frozenset[str] = field(
        default_factory=lambda: frozenset({"LOSSLESS", "HI_RES_LOSSLESS"})
    )
    priority: int = 0
    enabled: bool = True
    healthy: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "ExtensionManifest":
        extension_id = str(data.get("id") or data.get("name") or "").strip()
        version = str(data.get("version") or "").strip()
        if not extension_id:
            raise ValueError("extension manifest requires id or name")
        if not version:
            raise ValueError("extension manifest requires version")

        declared = data.get("capabilities", {})
        if isinstance(declared, dict):
            capabilities = frozenset(
                str(key) for key, value in declared.items() if value
            )
        else:
            capabilities = frozenset(str(value) for value in (declared or ()))
        if not capabilities and "download_provider" in data.get("type", []):
            capabilities = frozenset({"download"})

        qualities = frozenset(
            str(value).upper()
            for value in data.get("qualities", ("LOSSLESS", "HI_RES_LOSSLESS"))
        )
        return cls(
            id=extension_id,
            version=version,
            capabilities=capabilities,
            qualities=qualities,
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            healthy=bool(data.get("healthy", True)),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "capabilities": {name: True for name in sorted(self.capabilities)},
            "qualities": sorted(self.qualities),
            "priority": self.priority,
            "enabled": self.enabled,
            "healthy": self.healthy,
        }

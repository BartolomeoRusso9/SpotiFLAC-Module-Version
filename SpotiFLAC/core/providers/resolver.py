from __future__ import annotations

from dataclasses import dataclass, field

from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.providers.manifest import ExtensionManifest


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset({"download"}))
    qualities: frozenset[str] = field(
        default_factory=lambda: frozenset({"LOSSLESS", "HI_RES_LOSSLESS"})
    )
    priority: int = 0
    healthy: bool = True
    enabled: bool = True

    @classmethod
    def from_manifest(cls, manifest: dict, *, name: str | None = None) -> "ProviderProfile":
        compatible = dict(manifest)
        if name and not compatible.get("id") and not compatible.get("name"):
            compatible["id"] = name
        compatible.setdefault("version", "0.0.0")
        parsed = ExtensionManifest.from_dict(compatible)
        return cls(
            name=name or parsed.id,
            capabilities=parsed.capabilities,
            qualities=parsed.qualities,
            priority=parsed.priority,
            healthy=parsed.healthy,
            enabled=parsed.enabled,
        )

    def supports(self, quality: str) -> bool:
        return self.enabled and self.healthy and "download" in self.capabilities and (
            quality in self.qualities or "*" in self.qualities
        )


@dataclass(frozen=True)
class ProviderCandidate:
    name: str
    priority: int
    capabilities: frozenset[str]
    qualities: frozenset[str]


class ProviderResolver:
    """Resolve enabled, healthy providers by capability and priority."""

    _provider_priority = ["tidal", "qobuz", "deezer", "apple", "amazon"]

    def __init__(self, profiles: list[ProviderProfile] | None = None) -> None:
        self._profiles = {profile.name: profile for profile in profiles or []}

    @classmethod
    def from_extensions(cls, extensions: list[object]) -> "ProviderResolver":
        profiles = []
        for extension in extensions:
            manifest = getattr(extension, "manifest", None)
            if isinstance(manifest, dict):
                profiles.append(
                    ProviderProfile.from_manifest(
                        manifest,
                        name=getattr(extension, "name", None),
                    )
                )
        return cls(profiles)

    def resolve(self, request: DownloadRequest) -> list[str]:
        return [candidate.name for candidate in self.resolve_candidates(request)]

    def resolve_candidates(self, request: DownloadRequest) -> list[ProviderCandidate]:
        quality = request.config.download.quality.upper()
        if not self._profiles:
            return [
                ProviderCandidate(
                    name=name,
                    priority=0,
                    capabilities=frozenset({"download"}),
                    qualities=frozenset({"LOSSLESS", "HI_RES_LOSSLESS"}),
                )
                for name in self._provider_priority
            ]

        configured_order = {
            name: index for index, name in enumerate(self._provider_priority)
        }
        candidates = [
            profile for profile in self._profiles.values() if profile.supports(quality)
        ]
        candidates.sort(
            key=lambda profile: (
                -profile.priority,
                configured_order.get(profile.name, len(configured_order)),
                profile.name,
            )
        )
        return [
            ProviderCandidate(
                name=profile.name,
                priority=profile.priority,
                capabilities=profile.capabilities,
                qualities=profile.qualities,
            )
            for profile in candidates
        ]
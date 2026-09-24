from __future__ import annotations

from dataclasses import dataclass, field

from SpotiFLAC.core.config import DownloadRequest


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
        declared = manifest.get("capabilities", {})
        if isinstance(declared, dict):
            capabilities = frozenset(
                key for key, enabled in declared.items() if enabled
            )
        else:
            capabilities = frozenset(declared or ())
        if not capabilities and "download_provider" in manifest.get("type", []):
            capabilities = frozenset({"download"})

        qualities = frozenset(
            str(value).upper()
            for value in manifest.get("qualities", ("LOSSLESS", "HI_RES_LOSSLESS"))
        )
        return cls(
            name=name or manifest.get("id") or manifest.get("name", "unknown"),
            capabilities=capabilities,
            qualities=qualities,
            priority=int(manifest.get("priority", 0)),
            healthy=bool(manifest.get("healthy", True)),
            enabled=bool(manifest.get("enabled", True)),
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
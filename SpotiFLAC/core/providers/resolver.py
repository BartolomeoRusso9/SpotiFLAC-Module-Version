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

    def supports(self, quality: str) -> bool:
        return self.enabled and self.healthy and "download" in self.capabilities and (
            quality in self.qualities or "*" in self.qualities
        )


class ProviderResolver:
    """Resolve enabled, healthy providers by capability and priority."""

    _provider_priority = ["tidal", "qobuz", "deezer", "apple", "amazon"]

    def __init__(self, profiles: list[ProviderProfile] | None = None) -> None:
        self._profiles = {profile.name: profile for profile in profiles or []}

    def resolve(self, request: DownloadRequest) -> list[str]:
        quality = request.config.download.quality.upper()
        if not self._profiles:
            return list(self._provider_priority)

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
        return [profile.name for profile in candidates]
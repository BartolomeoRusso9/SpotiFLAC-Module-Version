from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from SpotiFLAC.core.providers import ExtensionManifest


@dataclass(frozen=True)
class ConformanceReport:
    """Network-free result of validating an extension boundary."""

    manifest: ExtensionManifest | None
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors


def validate_extension(
    manifest: Mapping[str, Any] | ExtensionManifest,
    provider: object,
) -> ConformanceReport:
    """Validate the minimum manifest and provider contract.

    Runtime-specific extensions may add capabilities, settings, and health
    methods, but every downloadable provider must expose a stable name and the
    asynchronous provider operation consumed by ``ProviderExecutor``.
    """
    errors: list[str] = []
    parsed: ExtensionManifest | None

    if isinstance(manifest, ExtensionManifest):
        parsed = manifest
    else:
        try:
            parsed = ExtensionManifest.from_dict(dict(manifest))
        except (TypeError, ValueError) as exc:
            parsed = None
            errors.append(f"manifest: {exc}")

    name = getattr(provider, "name", None)
    if not isinstance(name, str) or not name.strip():
        errors.append("provider: name must be a non-empty string")

    if not callable(getattr(provider, "download_track_async", None)):
        errors.append("provider: download_track_async is required")

    if parsed is not None and "download" not in parsed.capabilities:
        errors.append("manifest: download capability is required")

    return ConformanceReport(parsed, tuple(errors))

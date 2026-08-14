"""Extension-first service discovery and backwards-compatible aliases."""

from __future__ import annotations

from .manager import ExtensionManager

# These are public names historically accepted by the CLI/API.  They are now
# aliases only; no Python provider is instantiated for them.
SERVICE_ALIASES = {
    "tidal": "tidal-web",
    "qobuz": "qobuz-web",
    "amazon": "amazon",
    "apple": "apple-music",
    "deezer": "deezer",
    "soundcloud": "soundcloud",
    "youtube": "ytmusic-spotiflac",
    "pandora": "pandora",
}


def extension_id(service: str, manager: ExtensionManager | None = None) -> str:
    """Resolve ``ext:<id>`` and legacy service names to an installed extension.

    An installed extension with the exact service name wins over the historical
    alias.  This makes third-party extensions first-class without hard-coding
    them in the application.
    """
    value = service.removeprefix("ext:")
    if manager and manager.get_installed(value):
        return value
    return SERVICE_ALIASES.get(value, value)


def known_service(service: str) -> bool:
    """Return whether a CLI service spelling is valid without doing I/O."""
    return service.startswith("ext:") or service in SERVICE_ALIASES

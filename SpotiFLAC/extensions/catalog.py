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


def canonical_service_name(ext_name: str) -> str | None:
    """Normalize extension IDs such as ``tidal-web`` and ``tidal-py`` to one service name.

    Two extensions can provide one service — the JS and the Python Tidal
    providers both mean "Tidal" to someone choosing a source — so anything
    that offers the user a list of *services* has to collapse them first.
    """
    value = (ext_name or "").lower().removeprefix("ext:")
    if not value:
        return None

    value = value.replace("_", "-")
    value = value.replace("-web", "").replace("-py", "")

    alias_reverse = {v.lower(): k for k, v in SERVICE_ALIASES.items()}
    if value in alias_reverse:
        return alias_reverse[value]

    for prefix, service in (
        ("ytmusic", "youtube"),
        ("apple", "apple"),
        ("tidal", "tidal"),
        ("qobuz", "qobuz"),
        ("deezer", "deezer"),
        ("soundcloud", "soundcloud"),
        ("pandora", "pandora"),
        ("amazon", "amazon"),
    ):
        if value.startswith(prefix):
            return service
    return value


def installed_download_services(
    manager: ExtensionManager | None = None,
) -> list[dict[str, object]]:
    """The download services this install can actually offer, one per row.

    The single source of truth for every "choose your providers" surface:
    the interactive wizard, and the Settings list in the GUI. Both used to
    answer the question their own way — the wizard from the installed
    extensions, the GUI from a list hard-coded in app.js — so a fresh
    install offered twelve services in Settings and only the installed ones
    on the command line, and a third-party provider appeared in neither.

    Returns ``[{"id", "label", "extensions": [ext ids]}]``, sorted by id.
    Never raises: a broken extension directory means an empty list and a
    caller that says so, not a UI that cannot open.
    """
    try:
        ext_manager = manager or ExtensionManager(auto_install_downloads=False)
        installed = ext_manager.list_installed()
    except Exception:
        return []

    services: dict[str, list[str]] = {}
    for ext in installed:
        if not getattr(ext, "is_download_provider", False):
            continue
        service = canonical_service_name(getattr(ext, "name", ""))
        if not service:
            continue
        services.setdefault(service, []).append(str(getattr(ext, "name", "")))

    return [
        {"id": service, "label": service_label(service), "extensions": sorted(names)}
        for service, names in sorted(services.items())
    ]


def installed_service_ids(manager: ExtensionManager | None = None) -> list[str]:
    """Just the ids from `installed_download_services()`, for menus.

    Every guided frontend needs the same thing — a list of provider ids to
    offer — and each one used to unwrap the rows itself. One function, so a
    provider that appears in one menu appears in all of them.
    """
    return [str(service["id"]) for service in installed_download_services(manager)]


#: Spellings a title-cased id gets wrong.
_SERVICE_LABELS = {
    "amazon": "Amazon Music",
    "apple": "Apple Music",
    "gdstudio": "GDStudio",
    "joox": "JOOX",
    "netease": "NetEase",
    "qobuz": "Qobuz",
    "qq": "QQ Music",
    "soundcloud": "SoundCloud",
    "tidal": "Tidal",
    "youtube": "YouTube Music",
}


def service_label(service: str) -> str:
    """A service id as it should read in a menu."""
    if service in _SERVICE_LABELS:
        return _SERVICE_LABELS[service]
    return " ".join(part.capitalize() for part in service.replace("-", " ").split())

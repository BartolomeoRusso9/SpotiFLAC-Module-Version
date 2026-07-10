"""
SpotiFLAC — Extension System
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Permette di installare ed eseguire estensioni JS (.spotiflac-ext)
compatibili con SpotiFLAC Mobile, direttamente nel modulo Python.

Esempio base:
    from SpotiFLAC.extensions import ExtensionManager, JSExtensionProvider

    # 1. Installa un'estensione dal registry
    em = ExtensionManager()
    em.install("soundcloud")

    # 2. Usala come provider in un download
    provider = JSExtensionProvider("soundcloud")
    result = provider.download_track(metadata, output_dir="/tmp/music")
    print(result.file_path)

    # 3. Oppure passa "ext:soundcloud" direttamente a SpotiFLAC
    from SpotiFLAC import SpotiFLAC
    sf = SpotiFLAC(services=["ext:soundcloud", "tidal"])
    sf.download("https://open.spotify.com/track/...")

Requisiti:
    - Node.js ≥ 16  (comando 'node' nel PATH)
    - Estensione installata via ExtensionManager
"""

from .manager  import ExtensionManager, InstalledExtension, RegistryEntry, REGISTRY_URL
from .runtime  import JSRuntime, ExtensionRuntimeError
from .provider import JSExtensionProvider, make_extension_provider

__all__ = [
    # Manager
    "ExtensionManager",
    "InstalledExtension",
    "RegistryEntry",
    "REGISTRY_URL",
    # Runtime
    "JSRuntime",
    "ExtensionRuntimeError",
    # Provider
    "JSExtensionProvider",
    "make_extension_provider",
]

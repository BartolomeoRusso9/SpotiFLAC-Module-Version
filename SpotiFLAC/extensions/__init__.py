"""SpotiFLAC — Extension System.
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Allows installing and running JS extensions (.spotiflac-ext)
compatible with SpotiFLAC Mobile, directly in the Python module.

Basic example:
    from SpotiFLAC.extensions import ExtensionManager, JSExtensionProvider

    # 1. Install an extension from the registry
    em = ExtensionManager()
    em.install("soundcloud")

    # 2. Use it as a provider for a download
    provider = JSExtensionProvider("soundcloud")
    result = provider.download_track(metadata, output_dir="/tmp/music")
    print(result.file_path)

    # 3. Or pass "ext:soundcloud" directly to SpotiFLAC
    from SpotiFLAC import SpotiFLAC
    sf = SpotiFLAC(services=["ext:soundcloud", "tidal"])
    sf.download("https://open.spotify.com/track/...")

Requirements:
    - Node.js ≥ 16  ('node' command in PATH)
    - Extension installed via ExtensionManager
"""

from .manager import REGISTRY_URL, ExtensionManager, InstalledExtension, RegistryEntry
from .provider import JSExtensionProvider, make_extension_provider
from .runtime import ExtensionRuntimeError, JSRuntime

__all__ = [
    "REGISTRY_URL",
    # Manager
    "ExtensionManager",
    "ExtensionRuntimeError",
    "InstalledExtension",
    # Provider
    "JSExtensionProvider",
    # Runtime
    "JSRuntime",
    "RegistryEntry",
    "make_extension_provider",
]

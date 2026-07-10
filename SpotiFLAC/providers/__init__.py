from .amazon import AmazonProvider
from .apple_music import AppleMusicProvider
from .base import BaseProvider
from .deezer import DeezerProvider
from .gdstudio import JooxProvider, KuwoProvider, MiguProvider, NeteaseProvider
from .pandora import PandoraProvider
from .qobuz import QobuzProvider
from .soundcloud import SoundCloudProvider
from .spotify_metadata import SpotifyMetadataClient, parse_spotify_url
from .tidal import TidalProvider
from .youtube import YouTubeProvider

__all__ = [
    "AmazonProvider",
    "AppleMusicProvider",
    "BaseProvider",
    "DeezerProvider",
    "JooxProvider",
    "KuwoProvider",
    "MiguProvider",
    "NeteaseProvider",
    "PandoraProvider",
    "QobuzProvider",
    "SoundCloudProvider",
    "SpotifyMetadataClient",
    "TidalProvider",
    "YouTubeProvider",
    "parse_spotify_url",
    "PROVIDER_REGISTRY",
    "NATIVE_TO_EXTENSION_ID",
]

PROVIDER_REGISTRY: dict[str, type] = {
    "tidal": TidalProvider,
    "joox": JooxProvider,
    "netease": NeteaseProvider,
    "amazon": AmazonProvider,
    "apple": AppleMusicProvider,
    "deezer": DeezerProvider,
    "kuwo": KuwoProvider,
    "migu": MiguProvider,
    "pandora": PandoraProvider,
    "qobuz": QobuzProvider,
    "soundcloud": SoundCloudProvider,
    "youtube": YouTubeProvider,
}

# Maps native provider names to their corresponding extension IDs
# for auto-pairing extensions as fallback providers
NATIVE_TO_EXTENSION_ID: dict[str, str] = {
    "tidal": "tidal",
    "qobuz": "qobuz",
    "amazon": "amazon",
    "apple": "apple",
    "deezer": "deezer",
    "soundcloud": "soundcloud",
    "youtube": "youtube",
    "pandora": "pandora",
}

def _build_ext_provider(name: str, **kwargs) -> "BaseProvider | None":
    """
    Factory for providers with 'ext:' prefix.
    Example: name='ext:soundcloud' creates JSExtensionProvider('soundcloud').

    Optional parameters passable via kwargs:
        ext_settings    – dict of settings for the extension
        ext_dir         – extensions directory (default ~/.spotiflac/extensions)
        node_executable – path to Node.js (default 'node')
        timeout_s       – timeout for JS calls (default 120)
    """
    try:
        from ..extensions.provider import JSExtensionProvider
    except ImportError as e:
        import logging
        logging.getLogger(__name__).error(
            "Failed to import extensions module: %s", e
        )
        return None

    ext_id = name.removeprefix("ext:")
    return JSExtensionProvider(
        ext_id          = ext_id,
        settings        = kwargs.pop("ext_settings", None),
        ext_dir         = kwargs.pop("ext_dir", None),
        node_executable = kwargs.pop("node_executable", "node"),
        timeout_s       = kwargs.pop("timeout_s", 120),
    )
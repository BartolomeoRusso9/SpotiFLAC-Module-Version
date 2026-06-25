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
]

PROVIDER_REGISTRY: dict[str, type] = {
        "tidal":      TidalProvider,
        "joox":       JooxProvider,
        "netease":    NeteaseProvider,
    "amazon":     AmazonProvider,
    "apple":      AppleMusicProvider,
    "deezer":     DeezerProvider,
    "kuwo":       KuwoProvider,
    "migu":       MiguProvider,
    "pandora":    PandoraProvider,
    "qobuz":      QobuzProvider,
    "soundcloud": SoundCloudProvider,
    "youtube":    YouTubeProvider,
}

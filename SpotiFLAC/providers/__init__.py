from .base import BaseProvider
from .qobuz import QobuzProvider
from .tidal import TidalProvider
from .amazon import AmazonProvider
from .deezer import DeezerProvider
from .apple_music import AppleMusicProvider
from .soundcloud import SoundCloudProvider
from .youtube import YouTubeProvider
from .pandora import PandoraProvider
from .spotify_metadata import SpotifyMetadataClient, parse_spotify_url
from .joox import JooxProvider
from .netease import NeteaseProvider
from .migu import MiguProvider
from .kuwo import KuwoProvider
from .flacdownloader import FlacDownloaderProvider

__all__ = [
    "BaseProvider",
    "QobuzProvider",
    "TidalProvider",
    "AmazonProvider",
    "SpotiDownloaderProvider",
    "AppleMusicProvider",
    "SoundCloudProvider",
    "YouTubeProvider",
    "DeezerProvider",
    "PandoraProvider",
    "SpotifyMetadataClient",
    "parse_spotify_url",
    "FlacDownloaderProvider",
]

PROVIDER_REGISTRY: dict[str, type] = {
        "tidal":      TidalProvider,
        "joox":       JooxProvider,
        "netease":    NeteaseProvider,
        "migu":       MiguProvider,
        "kuwo":       KuwoProvider,
        "qobuz":      QobuzProvider,
        "amazon":     AmazonProvider,
        "deezer":     DeezerProvider,
        "apple":      AppleMusicProvider,
        "soundcloud": SoundCloudProvider,
        "youtube":    YouTubeProvider,
        "pandora":    PandoraProvider,
        "flacdownloader": FlacDownloaderProvider,
    }
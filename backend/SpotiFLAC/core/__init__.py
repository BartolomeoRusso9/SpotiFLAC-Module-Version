from .errors import (
    SpotiflacError, ErrorKind,
    AuthError, TrackNotFoundError, RateLimitedError,
    NetworkError, ParseError, InvalidUrlError,
)
from .models import TrackMetadata, DownloadResult, build_filename, sanitize
from .http import HttpClient, RetryConfig
from .tagger import embed_metadata, max_resolution_spotify_cover
from .progress import DownloadManager, ProgressCallback, RichProgressCallback

__all__ = [
    "SpotiflacError", "ErrorKind",
    "AuthError", "TrackNotFoundError", "RateLimitedError",
    "NetworkError", "ParseError", "InvalidUrlError",
    "TrackMetadata", "DownloadResult", "build_filename", "sanitize",
    "HttpClient", "RetryConfig",
    "embed_metadata", "max_resolution_spotify_cover",
    "DownloadManager", "ProgressCallback", "RichProgressCallback",
]
from .provider_stats import record_success, record_failure, prioritize as prioritize_providers

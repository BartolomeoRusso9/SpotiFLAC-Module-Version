from .errors import (
    AuthError,
    ErrorKind,
    InvalidUrlError,
    NetworkError,
    ParseError,
    RateLimitedError,
    SpotiflacError,
    TrackNotFoundError,
)
from .health_check import run_health_check
from .http import AsyncHttpClient, AsyncRateLimiter, NetworkManager, RetryConfig
from .lyrics import fetch_lyrics_async
from .metadata_enrichment import enrich_metadata_async
from .models import DownloadResult, TrackMetadata, build_filename, sanitize
from .progress import DownloadManager, ProgressCallback, RichProgressCallback
from .provider_stats import (
    prioritize_async as prioritize_providers_async,
)
from .provider_stats import (
    record_failure_async,
    record_success_async,
)
from .tagger import embed_metadata_async, max_resolution_spotify_cover

__all__ = [
    "AsyncHttpClient",
    "AsyncRateLimiter",
    "AuthError",
    "DownloadManager",
    "DownloadResult",
    "ErrorKind",
    "InvalidUrlError",
    "NetworkError",
    "NetworkManager",
    "ParseError",
    "ProgressCallback",
    "RateLimitedError",
    "RetryConfig",
    "RichProgressCallback",
    "SpotiflacError",
    "TrackMetadata",
    "TrackNotFoundError",
    "build_filename",
    "embed_metadata_async",
    "enrich_metadata_async",
    "fetch_lyrics_async",
    "max_resolution_spotify_cover",
    "prioritize_providers_async",
    "record_failure_async",
    "record_success_async",
    "run_health_check",
    "sanitize",
]

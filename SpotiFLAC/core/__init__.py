from .errors import (
    SpotiflacError, ErrorKind,
    AuthError, TrackNotFoundError, RateLimitedError,
    NetworkError, ParseError, InvalidUrlError,
)
from .models import TrackMetadata, DownloadResult, build_filename, sanitize
from .http import RetryConfig, AsyncHttpClient, AsyncRateLimiter, NetworkManager
from .tagger import embed_metadata_async, max_resolution_spotify_cover
from .lyrics import fetch_lyrics_async
from .metadata_enrichment import enrich_metadata_async
from .health_check import run_health_check
from .progress import DownloadManager, ProgressCallback, RichProgressCallback
from .provider_stats import (
    record_success_async,
    record_failure_async,
    prioritize_async as prioritize_providers_async,
)

__all__ = [
    "SpotiflacError", "ErrorKind",
    "AuthError", "TrackNotFoundError", "RateLimitedError",
    "NetworkError", "ParseError", "InvalidUrlError",
    "TrackMetadata", "DownloadResult", "build_filename", "sanitize",
    "RetryConfig", "AsyncHttpClient", "AsyncRateLimiter", "NetworkManager",
    "embed_metadata_async", "fetch_lyrics_async", "enrich_metadata_async",
    "run_health_check", "max_resolution_spotify_cover",
    "DownloadManager", "ProgressCallback", "RichProgressCallback",
    "record_success_async", "record_failure_async", "prioritize_providers_async",
]

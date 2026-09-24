from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from SpotiFLAC.core.models import DownloadResult, TrackMetadata


@dataclass
class OutputConfig:
    directory: Path = Path("./Downloads")
    filename_format: str = "{title} - {artist}"
    artist_subfolders: bool = False
    album_subfolders: bool = False
    playlist_subfolders: bool = True


@dataclass
class DownloadConfig:
    quality: str = "LOSSLESS"
    services: list[str] = field(default_factory=list)
    allow_fallback: bool = True
    max_concurrent: int = 2
    retries: int = 0
    timeout: int | None = None
    resume: bool = True


@dataclass
class MetadataConfig:
    enrich: bool = True
    providers: list[str] = field(
        default_factory=lambda: ["deezer", "apple", "qobuz", "tidal"],
    )


@dataclass
class LyricsConfig:
    enabled: bool = True
    providers: list[str] = field(
        default_factory=lambda: ["spotify", "apple", "musixmatch", "lrclib", "amazon"],
    )
    save_lrc: bool = False
    word_by_word: bool = True


@dataclass
class ExtensionConfig:
    registries: list[str] = field(default_factory=list)
    auto_update: bool = True
    minimum_trust: str = "UNVERIFIED"


@dataclass
class QueueConfig:
    persistent: bool = True
    max_jobs: int = 100
    retry_on_error: bool = True


@dataclass
class SecurityConfig:
    require_https: bool = True
    allow_unsigned_local: bool = True
    allow_unsigned_remote: bool = False


@dataclass
class SpotiFLACConfig:
    output: OutputConfig = field(default_factory=OutputConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    lyrics: LyricsConfig = field(default_factory=LyricsConfig)
    extensions: ExtensionConfig = field(default_factory=ExtensionConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)

    @classmethod
    def from_legacy_options(cls, options: Any) -> "SpotiFLACConfig":
        """Build the new config model from the current downloader options."""
        config = cls()
        config.output.directory = Path(getattr(options, "output_dir", "./Downloads"))
        config.output.filename_format = getattr(
            options, "filename_format", "{title} - {artist}"
        )
        config.output.artist_subfolders = getattr(
            options, "use_artist_subfolders", False
        )
        config.output.album_subfolders = getattr(options, "use_album_subfolders", False)
        config.output.playlist_subfolders = getattr(
            options, "create_playlist_subfolders", True
        )

        config.download.quality = getattr(options, "quality", "LOSSLESS")
        config.download.services = list(getattr(options, "services", []))
        config.download.allow_fallback = getattr(options, "allow_fallback", True)
        config.download.max_concurrent = getattr(options, "max_concurrent_downloads", 2)
        config.download.retries = getattr(options, "track_max_retries", 0)
        config.download.timeout = getattr(options, "timeout_s", None)
        config.download.resume = getattr(options, "resume", True)

        config.metadata.enrich = getattr(options, "enrich_metadata", True)
        config.metadata.providers = list(
            getattr(options, "enrich_providers", config.metadata.providers)
        )

        config.lyrics.enabled = getattr(options, "embed_lyrics", True)
        config.lyrics.providers = list(
            getattr(options, "lyrics_providers", config.lyrics.providers)
        )
        config.lyrics.save_lrc = getattr(options, "save_lrc", False)
        config.lyrics.word_by_word = getattr(options, "apple_lyrics_word_by_word", True)

        config.extensions.registries = list(getattr(options, "registries", []))
        config.extensions.auto_update = True

        config.queue.persistent = True
        config.queue.max_jobs = 100
        config.queue.retry_on_error = True

        return config


@dataclass
class DownloadRequest:
    sources: list[str]
    config: SpotiFLACConfig
    prefetched: dict[str, TrackMetadata] | None = None


@dataclass
class DownloadSkip:
    track: TrackMetadata | None = None
    reason: str = ""
    provider: str | None = None


@dataclass
class DownloadFailure:
    track: TrackMetadata | None = None
    source: str | None = None
    reason: str = ""
    provider: str | None = None
    attempts: int = 0
    retryable: bool = False


@dataclass
class DownloadReport:
    succeeded: list[DownloadResult]
    failed: list[DownloadFailure]
    skipped: list[DownloadSkip] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def total(self) -> int:
        return len(self.succeeded) + len(self.failed) + len(self.skipped)

    @property
    def success_count(self) -> int:
        return len(self.succeeded)


__all__ = [
    "DownloadConfig",
    "DownloadFailure",
    "DownloadReport",
    "DownloadRequest",
    "DownloadSkip",
    "ExtensionConfig",
    "LyricsConfig",
    "MetadataConfig",
    "OutputConfig",
    "QueueConfig",
    "SecurityConfig",
    "SpotiFLACConfig",
]

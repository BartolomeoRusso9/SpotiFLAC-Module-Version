"""webapi/schemas.py — the request and response shapes of `/api/v1`.

These are the contract. The RPC bridge hands back whatever a GUI method
happened to return; everything here is declared, validated on the way in, and
rendered into the OpenAPI document FastAPI serves at `/docs` — which is what
makes the surface something a client can be generated against rather than
reverse-engineered.

Field names deliberately match `core/models.TrackMetadata` where they overlap,
so someone reading both does not have to hold two vocabularies at once.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ─────────────────────────────────────────────────────────────
#  Shared
# ─────────────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """Every non-2xx body. One shape, so a client can parse failures once."""

    error: str = Field(description="Human-readable summary of what went wrong.")
    detail: str | None = Field(
        default=None, description="Extra context, when there is any worth giving."
    )


class ApiInfo(BaseModel):
    name: str = "SpotiFLAC"
    version: str = Field(description="The installed SpotiFLAC version.")
    api_version: str = "v1"
    multiuser: bool
    authenticated: bool = Field(
        description="Whether this instance requires a token or a login."
    )


# ─────────────────────────────────────────────────────────────
#  Metadata
# ─────────────────────────────────────────────────────────────


class TrackOut(BaseModel):
    id: str
    title: str
    artists: str
    album: str
    album_artist: str = ""
    isrc: str = ""
    duration_ms: int = 0
    track_number: int = 0
    disc_number: int = 1
    release_date: str = ""
    cover_url: str = ""
    external_url: str = ""
    is_explicit: bool = False

    @classmethod
    def from_metadata(cls, metadata: Any) -> TrackOut:
        return cls(
            id=getattr(metadata, "id", "") or "",
            title=getattr(metadata, "title", "") or "",
            artists=getattr(metadata, "artists", "") or "",
            album=getattr(metadata, "album", "") or "",
            album_artist=getattr(metadata, "album_artist", "") or "",
            isrc=getattr(metadata, "isrc", "") or "",
            duration_ms=int(getattr(metadata, "duration_ms", 0) or 0),
            track_number=int(getattr(metadata, "track_number", 0) or 0),
            disc_number=int(getattr(metadata, "disc_number", 1) or 1),
            release_date=getattr(metadata, "release_date", "") or "",
            cover_url=getattr(metadata, "cover_url", "") or "",
            external_url=getattr(metadata, "external_url", "") or "",
            is_explicit=bool(getattr(metadata, "is_explicit", False)),
        )


class ResolveRequest(BaseModel):
    url: str = Field(
        description="A Spotify track/album/playlist/artist URL or URI. Other "
        "platforms resolve through core/link_resolver.",
        min_length=1,
    )
    include_featuring: bool = Field(
        default=True,
        description="For an artist URL, whether to include releases they only "
        "appear on.",
    )

    @field_validator("url")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "url cannot be blank"
            raise ValueError(msg)
        return stripped


class ResolveResponse(BaseModel):
    name: str = Field(description="Album/playlist/artist name, or the track title.")
    kind: str = Field(description="track | album | playlist | artist | …")
    total: int
    tracks: list[TrackOut]


class SearchResponse(BaseModel):
    query: str
    tracks: list[TrackOut] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────
#  Downloads
# ─────────────────────────────────────────────────────────────


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, description="What to download.")
    quality: str = Field(
        default="LOSSLESS",
        description="Canonical tier name; see core/quality.normalize_quality.",
    )
    services: list[str] | None = Field(
        default=None,
        description="Provider/extension ids to try, in order. Omit for the "
        "instance default.",
    )
    output_dir: str | None = Field(
        default=None,
        description="Ignored in multi-user mode, where a download always lands "
        "in the calling account's own folder.",
    )

    @field_validator("url")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "url cannot be blank"
            raise ValueError(msg)
        return stripped


class JobOut(BaseModel):
    id: str
    owner: str = ""
    status: Literal["queued", "running", "done", "failed"]
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    payload: dict = Field(default_factory=dict)

    @classmethod
    def from_job(cls, job: Any) -> JobOut:
        return cls(**job.to_dict())


class JobListResponse(BaseModel):
    jobs: list[JobOut]


class DownloadRecordOut(BaseModel):
    id: int
    title: str
    artist: str
    album: str
    isrc: str
    provider: str
    format: str
    bytes: int
    success: bool
    downloaded_at: float
    # Deliberately no `file_path`: it is a path on the server, useless to a
    # remote caller and a disclosure of the host's directory layout. The same
    # reasoning core/notifiers.py applies to what it sends outward.


class HistoryResponse(BaseModel):
    total: int
    downloads: list[DownloadRecordOut]


# ─────────────────────────────────────────────────────────────
#  Subscriptions
# ─────────────────────────────────────────────────────────────


class SubscriptionOut(BaseModel):
    id: str
    url: str
    kind: str
    name: str
    output_dir: str = ""
    owner: str = ""
    include_groups: str
    enabled: bool
    created_at: float
    last_checked_at: float | None = None
    last_error: str | None = None
    seen_count: int = 0


class SubscriptionCreate(BaseModel):
    url: str = Field(min_length=1, description="An artist URL to follow.")
    name: str = ""
    include_groups: str | None = Field(
        default=None, description="e.g. 'album,single' — or 'all'."
    )


class SubscriptionListResponse(BaseModel):
    subscriptions: list[SubscriptionOut]


class ReleaseOut(BaseModel):
    id: str
    title: str
    type: str
    year: str
    url: str


class SubscriptionCheckOut(BaseModel):
    subscription_id: str
    url: str
    artist: str
    total_releases: int
    new_releases: list[ReleaseOut]
    watermarked: bool = Field(
        description="True on a first check: the existing catalogue was recorded "
        "as seen rather than reported as new."
    )
    error: str = ""


class SubscriptionCheckResponse(BaseModel):
    checked: int
    new: int
    results: list[SubscriptionCheckOut]


# ─────────────────────────────────────────────────────────────
#  Extensions
# ─────────────────────────────────────────────────────────────


class ExtensionHealthOut(BaseModel):
    provider: str
    attempts: int
    successes: int
    failures: int
    success_rate: float | None
    avg_duration_s: float
    last_outcome: str = ""
    last_error: str = ""
    version: str | None = None
    installed: bool = False


class ExtensionHealthResponse(BaseModel):
    providers: list[ExtensionHealthOut]
    totals: dict


# ─────────────────────────────────────────────────────────────
#  Library
# ─────────────────────────────────────────────────────────────


class LibraryScanRequest(BaseModel):
    path: str = Field(min_length=1, description="Folder to scan, on the server.")
    target_quality: str = "LOSSLESS"
    recursive: bool = True
    verify_hires: bool = Field(
        default=False,
        description="Also flag files that declare Hi-Res but whose content "
        "stops at CD range. Decodes ~30s per file — slow on a big library.",
    )


class UpgradeCandidateOut(BaseModel):
    title: str
    artist: str
    album: str
    isrc: str
    current: str
    current_tier: str
    target_tier: str
    reason: str


class LibraryScanResponse(BaseModel):
    target: str
    scanned: int
    unreadable: int
    already_ok: int
    upgradable: int
    candidates: list[UpgradeCandidateOut]

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
#  CSV input
# ─────────────────────────────────────────────────────────────


class CsvResolveRequest(BaseModel):
    """A CSV's *contents*, not a path.

    A path would be a path on the server, which a remote caller neither knows
    nor should be able to name — the same reasoning that keeps `file_path`
    out of DownloadRecordOut.
    """

    content: str = Field(
        min_length=1,
        max_length=2_000_000,
        description="The file itself. Any delimiter; a header is detected.",
    )
    name: str = Field(
        default="",
        max_length=200,
        description="What to call the file in the response. Cosmetic.",
    )
    delimiter: str | None = Field(
        default=None,
        max_length=1,
        description="Overrides the automatic detection.",
    )
    min_score: float = Field(
        default=0.62,
        ge=0.0,
        le=1.0,
        description="How close a catalogue match must be before a text-only "
        "row is accepted. Rows below it come back unresolved rather than "
        "guessed at.",
    )


class CsvResolvedRow(BaseModel):
    line: int
    input: str
    url: str
    how: Literal["link", "search", "isrc"]
    score: float
    matched: str = ""


class CsvUnresolvedRow(BaseModel):
    line: int
    input: str
    reason: str
    best: str = ""
    score: float = 0.0


class CsvResolveResponse(BaseModel):
    """What the file turned out to contain, before anything is queued.

    Resolving and downloading are deliberately two calls: the caller sees
    every match (and every miss) first, then queues the URLs it accepts
    through `POST /downloads` like any other download — so quotas, the queue
    limit and per-account isolation all apply unchanged.
    """

    rows: int
    resolved: list[CsvResolvedRow]
    unresolved: list[CsvUnresolvedRow]
    urls: list[str] = Field(description="Every resolved link, deduplicated.")


# ─────────────────────────────────────────────────────────────
#  Dashboard
# ─────────────────────────────────────────────────────────────


class StatsWindow(BaseModel):
    since: float | None = None
    until: float | None = None
    label: str = Field(
        description="How the period reads to a person: 'all time', '2026', "
        "'last 30 days'."
    )


class StatsTotals(BaseModel):
    tracks: int
    failed: int
    attempts: int
    success_rate: float
    bytes: int
    artists: int
    albums: int
    listening_ms: int
    listening_known: int = Field(
        description="How many of the tracks carried a duration. Durations are "
        "only recorded from schema v2 onwards, so an older history reports "
        "less listening time than it actually holds."
    )


class StatsEntry(BaseModel):
    """One row of a ranking: an artist, a genre, a provider, a format."""

    name: str
    tracks: int
    share: float = Field(description="Fraction of the period's tracks, 0…1.")


class StatsAlbumEntry(BaseModel):
    name: str
    artist: str
    tracks: int
    share: float


class StatsTrackEntry(BaseModel):
    name: str
    artist: str
    tracks: int


class StatsCoverage(BaseModel):
    """A ranking that only some of the history could contribute to.

    `known` + `unknown` add up to the period's tracks; a client should say so
    rather than presenting `entries` as the whole picture.
    """

    known: int
    unknown: int
    entries: list[StatsEntry]


class StatsDecades(StatsCoverage):
    pass


class StatsMonth(BaseModel):
    month: str = Field(
        description="YYYY-MM. Months with nothing in them are "
        "present with zeroes rather than missing."
    )
    tracks: int
    bytes: int


class StatsBusiestDay(BaseModel):
    date: str
    tracks: int


class StatsActivity(BaseModel):
    by_weekday: list[int] = Field(description="Seven counts, Monday first.")
    by_hour: list[int] = Field(description="Twenty-four counts, local time.")
    busiest_day: StatsBusiestDay | None = None
    active_days: int
    longest_streak: int
    current_streak: int


class StatsMilestone(BaseModel):
    title: str
    artist: str
    album: str
    provider: str
    downloaded_at: float


class StatsResponse(BaseModel):
    """Everything the dashboard shows for one period and one account."""

    generated_at: float
    owner: str = ""
    window: StatsWindow
    totals: StatsTotals
    top_artists: list[StatsEntry]
    top_albums: list[StatsAlbumEntry]
    top_tracks: list[StatsTrackEntry] = Field(
        description="Tracks fetched more than once in the period — a repeat is "
        "worth surfacing precisely because it is not obvious."
    )
    top_genres: StatsCoverage
    decades: StatsDecades
    providers: list[StatsEntry]
    formats: list[StatsEntry]
    timeline: list[StatsMonth]
    activity: StatsActivity
    first: StatsMilestone | None = None
    last: StatsMilestone | None = None


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


class LibraryDuplicatesRequest(BaseModel):
    path: str = Field(min_length=1, description="Folder to scan, on the server.")
    recursive: bool = True
    match: Literal["isrc", "tags", "both"] = Field(
        default="both",
        description="Which signal decides that two files are one recording: "
        "'isrc' only, 'tags' only (artist/title + duration), or 'both' — "
        "ISRC first, tags for the files that carry none.",
    )
    duration_tolerance_s: float = Field(
        default=4.0,
        ge=0.0,
        le=600.0,
        description="How far two durations may differ and still be the same "
        "recording. Keeps a live take out of the studio cut's group.",
    )
    verify: bool = Field(
        default=False,
        description="Confirm each group against the audio with Chromaprint "
        "before reporting it. Needs the optional 'dedup' extra; if it is "
        "missing the scan says so in `notes` rather than failing.",
    )
    similarity_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    export_db: bool = Field(
        default=False,
        description="Also write a SQLite index of the whole library to "
        "`.spotiflac-duplicates/library-index.db` inside the scanned folder. "
        "Its path comes back in `database`.",
    )


class DuplicateFileOut(BaseModel):
    path: str
    size: int
    duration_ms: int
    title: str
    artist: str
    album: str
    isrc: str
    quality: str = Field(description="Human-readable codec/rate/depth summary.")
    tier: int
    lossless: bool


class DuplicateGroupOut(BaseModel):
    key: str
    matched_by: str = Field(description="'isrc', 'tags', 'isrc+tags' or 'tags+audio'.")
    label: str
    count: int
    reclaimable_bytes: int
    keep: DuplicateFileOut = Field(description="The copy worth keeping.")
    duplicates: list[DuplicateFileOut] = Field(
        description="The redundant copies. Nothing here has been touched — "
        "this endpoint only reads."
    )


class LibraryStatsOut(BaseModel):
    files: int
    total_bytes: int
    total_size: str
    unreadable: int
    missing_tags: int
    missing_isrc: int
    by_extension: dict[str, int]
    by_tier: dict[str, int]


class LibraryDuplicatesResponse(BaseModel):
    root: str
    match: str
    verified: bool = Field(
        description="Whether every group was confirmed against the audio. "
        "False when `verify` was not asked for, and when it was asked for "
        "but could not run — `notes` says which."
    )
    duration_tolerance_s: float
    elapsed_s: float
    cache_hits: int
    library: LibraryStatsOut = Field(description="The recap, duplicates aside.")
    groups: int
    duplicate_files: int
    reclaimable_bytes: int
    reclaimable: str
    notes: list[str]
    database: str = Field(
        default="", description="Path of the SQLite index, if one was asked for."
    )
    duplicate_groups: list[DuplicateGroupOut]

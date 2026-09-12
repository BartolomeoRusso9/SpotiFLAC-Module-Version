"""core/subscriptions.py — following an artist, and fetching only what's new.

`--watch` re-syncs one URL on a timer. That is the right tool for a playlist
you keep pointed at, and the wrong one for an artist: a discography URL
re-resolves the entire back catalogue every pass, and the only reason it
doesn't re-download it is that every provider skips a file already on disk.
Nothing anywhere remembers "I have already seen this release", so nothing can
answer the question people actually have — *what came out since last time?*

That memory is all this module adds. A subscription is a URL plus a set of
release ids already seen; checking it lists the artist's releases, subtracts
the ones on record, and hands back the remainder. The download itself is
still `SpotiflacDownloader`, called with the new releases' own URLs — this
module never downloads anything, which is what keeps it testable without a
network or an installed provider.

The first check
---------------
A brand-new subscription does **not** download the back catalogue. It records
what exists today as already-seen and returns nothing, so the first thing it
ever fetches is the first thing released after you subscribed. That is what
"follow an artist" means to almost everybody, and the alternative — typing
one command and being handed four hundred tracks — is a bad surprise to
inflict by default. `backfill=True` (CLI: `--subscribe-backfill`) opts into
the other reading.

Playlists
---------
A playlist URL makes a `kind="playlist"` subscription: the seen-set holds
track ids instead of release ids, and a check hands back the tracks added
since the last one — with their metadata, so they can be queued for download
without a second lookup each. The same first-check rule applies: the tracks
already in the playlist are the baseline.

Checking on a timer
-------------------
`interval_minutes` > 0 puts a subscription on the scheduler
(core/subscription_scheduler.py), which checks it whenever that much time has
passed since its last check and downloads what is new with the settings saved
in `download_config`. 0 leaves it to the manual "check" buttons.

Storage is `core/db.py`: the seen-set is append-mostly and unbounded, which
is the shape a rewrite-the-whole-file JSON store handles worst.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import db

logger = logging.getLogger(__name__)

#: What `include_groups` accepts, mirroring SpotifyMetadataClient's own
#: vocabulary (see get_artist_albums_async).
RELEASE_GROUPS = ("album", "single", "compilation", "appears_on")

DEFAULT_GROUPS = "album,single"

#: What a subscription can follow, keyed by parse_spotify_url()'s type.
KINDS = {"artist": "artist", "artist_discography": "artist", "playlist": "playlist"}

#: The shortest schedule accepted. Each check is several Spotify calls (a
#: whole playlist listing, for a playlist), and a dozen subscriptions checked
#: every minute is how an instance gets rate-limited.
MIN_INTERVAL_MINUTES = 15


class SubscriptionError(ValueError):
    """A subscription could not be created or resolved."""


@dataclass
class Release:
    """One item a subscription can find new: an artist's release, or — with
    `type="track"` — a track added to a followed playlist."""

    id: str
    title: str = ""
    type: str = "single"
    year: str = ""

    @property
    def url(self) -> str:
        path = "track" if self.type == "track" else "album"
        return f"https://open.spotify.com/{path}/{self.id}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "year": self.year,
            "url": self.url,
        }


@dataclass
class Subscription:
    id: str
    url: str
    kind: str = "artist"
    name: str = ""
    output_dir: str = ""
    owner: str = ""
    include_groups: str = DEFAULT_GROUPS
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_checked_at: float | None = None
    last_error: str | None = None
    interval_minutes: int = 0
    download_config: dict = field(default_factory=dict)
    #: Whether a check has ever succeeded for this subscription. The first
    #: one records what exists as the baseline; see check_async().
    baselined: bool = False

    @property
    def groups(self) -> list[str]:
        return [g.strip() for g in self.include_groups.split(",") if g.strip()]

    @property
    def next_check_at(self) -> float | None:
        """When the scheduler will next check this, or None if it won't."""
        if not self.enabled or self.interval_minutes <= 0:
            return None
        if self.last_checked_at is None:
            return time.time()
        return float(self.last_checked_at) + self.interval_minutes * 60

    def to_dict(self) -> dict:
        # download_config stays out: it is the page's own settings echoed
        # back, and nothing that lists subscriptions needs it.
        return {
            "id": self.id,
            "url": self.url,
            "kind": self.kind,
            "name": self.name,
            "output_dir": self.output_dir,
            "owner": self.owner,
            "include_groups": self.include_groups,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
            "interval_minutes": self.interval_minutes,
            "next_check_at": self.next_check_at,
            "seen_count": count_seen(self.id),
        }


def _load_config(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _row_to_subscription(row) -> Subscription:
    return Subscription(
        id=row["id"],
        url=row["url"],
        kind=row["kind"] or "artist",
        name=row["name"] or "",
        output_dir=row["output_dir"] or "",
        owner=row["owner"] or "",
        include_groups=row["include_groups"] or DEFAULT_GROUPS,
        enabled=bool(row["enabled"]),
        created_at=float(row["created_at"] or 0.0),
        last_checked_at=row["last_checked_at"],
        last_error=row["last_error"],
        interval_minutes=int(row["interval_minutes"] or 0),
        download_config=_load_config(row["download_config"]),
        baselined=bool(row["baselined"]),
    )


# ─────────────────────────────────────────────────────────────
#  Store — no network, no downloads
# ─────────────────────────────────────────────────────────────


def _normalise_groups(groups: str | None) -> str:
    if not groups:
        return DEFAULT_GROUPS
    wanted = [g.strip().lower() for g in str(groups).split(",") if g.strip()]
    if "all" in wanted:
        return ",".join(RELEASE_GROUPS)
    unknown = [g for g in wanted if g not in RELEASE_GROUPS]
    if unknown:
        msg = (
            f"Unknown release group(s): {', '.join(unknown)}. "
            f"Expected any of: {', '.join(RELEASE_GROUPS)} (or 'all')."
        )
        raise SubscriptionError(msg)
    return ",".join(wanted) or DEFAULT_GROUPS


def _normalise_interval(minutes) -> int:
    try:
        value = int(minutes or 0)
    except (TypeError, ValueError):
        msg = f"Check interval must be a number of minutes, not {minutes!r}"
        raise SubscriptionError(msg) from None
    if value <= 0:
        return 0
    if value < MIN_INTERVAL_MINUTES:
        msg = (
            f"Check interval must be at least {MIN_INTERVAL_MINUTES} minutes "
            "(or 0 to check only on demand)"
        )
        raise SubscriptionError(msg)
    return value


def kind_for_url(url: str) -> str:
    """ "artist" or "playlist" for a URL a subscription can follow."""
    from .spotify_metadata import parse_spotify_url

    try:
        url_type = parse_spotify_url(url)["type"]
    except Exception:
        url_type = ""
    kind = KINDS.get(url_type)
    if kind is None:
        msg = "Follow a Spotify artist or playlist link" + (
            f"; {url} is a {url_type}." if url_type else f"; {url} is neither."
        )
        raise SubscriptionError(msg)
    return kind


def add(
    url: str,
    *,
    name: str = "",
    output_dir: str = "",
    owner: str = "",
    include_groups: str | None = None,
    kind: str | None = None,
    interval_minutes: int | None = None,
    download_config: dict | None = None,
) -> Subscription:
    """Registers a subscription. Re-adding the same URL updates it in place.

    Keyed on the URL rather than on a generated id so `--subscribe <url>`
    twice is idempotent — which is what someone re-running a setup script
    expects, and the alternative is a silent duplicate that then downloads
    everything twice.

    `kind` is read off the URL when not given. `interval_minutes` and
    `download_config` left as None keep what an existing subscription has
    (a new one starts manual, with no saved settings).
    """
    url = (url or "").strip()
    if not url:
        msg = "A subscription needs a URL (an artist or playlist link)"
        raise SubscriptionError(msg)

    kind = kind or kind_for_url(url)
    groups = _normalise_groups(include_groups)
    interval = (
        None if interval_minutes is None else _normalise_interval(interval_minutes)
    )

    existing = get_by_url(url, owner=owner)
    if existing is not None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE subscriptions SET name = ?, output_dir = ?, "
                "include_groups = ?, enabled = 1, interval_minutes = ?, "
                "download_config = ? WHERE id = ?",
                (
                    name or existing.name,
                    output_dir or existing.output_dir,
                    groups,
                    existing.interval_minutes if interval is None else interval,
                    json.dumps(
                        existing.download_config
                        if download_config is None
                        else download_config
                    ),
                    existing.id,
                ),
            )
        refreshed = get(existing.id)
        assert refreshed is not None
        return refreshed

    sub = Subscription(
        id=uuid.uuid4().hex,
        url=url,
        kind=kind,
        name=name,
        output_dir=output_dir,
        owner=owner,
        include_groups=groups,
        interval_minutes=interval or 0,
        download_config=dict(download_config or {}),
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO subscriptions (id, kind, url, name, output_dir, owner, "
            "include_groups, enabled, created_at, interval_minutes, "
            "download_config) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                sub.id,
                sub.kind,
                sub.url,
                sub.name,
                sub.output_dir,
                sub.owner,
                sub.include_groups,
                sub.created_at,
                sub.interval_minutes,
                json.dumps(sub.download_config),
            ),
        )
    return sub


def remove(subscription_id: str) -> bool:
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM subscription_seen WHERE subscription_id = ?",
            (subscription_id,),
        )
        cursor = conn.execute(
            "DELETE FROM subscriptions WHERE id = ?", (subscription_id,)
        )
        return cursor.rowcount > 0


def remove_by_url(url: str, *, owner: str = "") -> bool:
    sub = get_by_url(url, owner=owner)
    return remove(sub.id) if sub is not None else False


def get(subscription_id: str) -> Subscription | None:
    row = (
        db.connection()
        .execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,))
        .fetchone()
    )
    return _row_to_subscription(row) if row is not None else None


def get_by_url(url: str, *, owner: str = "") -> Subscription | None:
    row = (
        db.connection()
        .execute(
            "SELECT * FROM subscriptions WHERE url = ? AND owner = ?", (url, owner)
        )
        .fetchone()
    )
    return _row_to_subscription(row) if row is not None else None


def list_all(
    *, owner: str | None = None, enabled_only: bool = False
) -> list[Subscription]:
    sql = "SELECT * FROM subscriptions"
    clauses: list[str] = []
    params: list = []
    if owner is not None:
        clauses.append("owner = ?")
        params.append(owner)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at"
    rows = db.connection().execute(sql, params).fetchall()
    return [_row_to_subscription(r) for r in rows]


def set_enabled(subscription_id: str, enabled: bool) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, subscription_id),
        )
        return cursor.rowcount > 0


def set_schedule(
    subscription_id: str,
    interval_minutes: int,
    download_config: dict | None = None,
) -> bool:
    """Puts a subscription on the scheduler (or takes it off, with 0).

    `download_config` replaces the saved download settings when given; the
    page sends its current ones, so a scheduled download is done the way the
    person who scheduled it had things set up.
    """
    interval = _normalise_interval(interval_minutes)
    with db.transaction() as conn:
        if download_config is None:
            cursor = conn.execute(
                "UPDATE subscriptions SET interval_minutes = ? WHERE id = ?",
                (interval, subscription_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE subscriptions SET interval_minutes = ?, download_config = ? "
                "WHERE id = ?",
                (interval, json.dumps(download_config), subscription_id),
            )
        return cursor.rowcount > 0


def update_download_config(owner: str, download_config: dict) -> int:
    """Saves `download_config` on every subscription `owner` has.

    Called whenever the page hands over its settings (a manual check, a
    schedule change), so scheduled downloads follow settings changed since.
    """
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET download_config = ? WHERE owner = ?",
            (json.dumps(download_config), owner),
        )
        return cursor.rowcount


def due(now: float | None = None) -> list[Subscription]:
    """Enabled, scheduled subscriptions whose interval has run out.

    Never checked counts as due: a schedule set a minute ago should not wait
    a whole interval before its first look.
    """
    now = time.time() if now is None else now
    rows = (
        db.connection()
        .execute(
            "SELECT * FROM subscriptions WHERE enabled = 1 AND interval_minutes > 0 "
            "AND (last_checked_at IS NULL OR last_checked_at + interval_minutes * 60 <= ?) "
            "ORDER BY created_at",
            (now,),
        )
        .fetchall()
    )
    return [_row_to_subscription(r) for r in rows]


def is_baselined(subscription_id: str) -> bool:
    """Whether a check has already succeeded for this subscription.

    Its own column rather than "the seen-set is non-empty": a listing can
    succeed and be empty (a playlist with nothing in it yet, an artist with
    no release in the chosen groups), and reading that as "never checked"
    made the next check watermark away the first thing added.
    """
    row = (
        db.connection()
        .execute(
            "SELECT baselined FROM subscriptions WHERE id = ?", (subscription_id,)
        )
        .fetchone()
    )
    return bool(row["baselined"]) if row is not None else False


def mark_baselined(subscription_id: str) -> None:
    """Records that a check succeeded, so later ones report what is new.

    Only successful checks call this: a failed one still stamps
    `last_checked_at` (see record_check), and must not be mistaken for the
    baseline.
    """
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET baselined = 1 WHERE id = ?", (subscription_id,)
        )


def record_check(subscription_id: str, error: str | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_checked_at = ?, last_error = ? WHERE id = ?",
            (time.time(), error, subscription_id),
        )


# ── The seen-set ──────────────────────────────────────────────────────────


def seen_ids(subscription_id: str) -> set[str]:
    rows = (
        db.connection()
        .execute(
            "SELECT release_id FROM subscription_seen WHERE subscription_id = ?",
            (subscription_id,),
        )
        .fetchall()
    )
    return {r["release_id"] for r in rows}


def count_seen(subscription_id: str) -> int:
    row = (
        db.connection()
        .execute(
            "SELECT COUNT(*) AS n FROM subscription_seen WHERE subscription_id = ?",
            (subscription_id,),
        )
        .fetchone()
    )
    return int(row["n"]) if row else 0


def mark_seen(subscription_id: str, releases: list[Release]) -> None:
    if not releases:
        return
    stamp = time.time()
    with db.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO subscription_seen "
            "(subscription_id, release_id, title, seen_at) VALUES (?, ?, ?, ?)",
            [(subscription_id, r.id, r.title, stamp) for r in releases],
        )


def forget_seen(subscription_id: str) -> None:
    """Empties the seen-set, so the next check treats everything as new.

    Backs `--subscribe-reset`: the way to say "actually, do fetch the back
    catalogue" after having already subscribed.
    """
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM subscription_seen WHERE subscription_id = ?",
            (subscription_id,),
        )


# ─────────────────────────────────────────────────────────────
#  Checking — this is the part that needs the network
# ─────────────────────────────────────────────────────────────


async def list_releases_async(
    url: str,
    include_groups: str = DEFAULT_GROUPS,
    *,
    client: Any = None,
) -> tuple[str, list[Release]]:
    """(artist name, releases) for a subscription's URL.

    Reads the discography listing only — it does not fetch each album's
    tracks, which `get_artist_albums_async` does and which is the expensive
    half. A subscription usually finds nothing new, and paying for the track
    listing of two hundred albums to discover that is the difference between
    a check that can run every hour and one that can't.

    `client` is injectable so tests can drive this without Spotify.
    """
    from .spotify_metadata import (
        SpotifyMetadataClient,
        _extract_discography_release,
        _extract_release_id,
        _normalize_release_type,
        parse_spotify_url,
    )

    info = parse_spotify_url(url)
    if info["type"] not in ("artist", "artist_discography"):
        msg = (
            f"Only an artist has a discography to list; {url} is a "
            f"{info['type']}. Use --watch for an album you want re-synced."
        )
        raise SubscriptionError(msg)

    artist_id = info["id"]
    metadata_client = client or SpotifyMetadataClient()

    profile = await metadata_client.get_artist_profile_async(artist_id)
    artist_name = (profile.get("profile") or {}).get("name", "") or ""

    import asyncio

    items = await asyncio.to_thread(
        metadata_client.web_client.get_artist_discography, artist_id
    )

    allowed = {g.strip() for g in include_groups.split(",") if g.strip()}
    releases: list[Release] = []
    seen: set[str] = set()

    for item in items or []:
        release = _extract_discography_release(item)
        release_id = _extract_release_id(release)
        if not release_id or release_id in seen:
            continue
        release_type = _normalize_release_type(release.get("type", ""))
        if allowed and release_type not in allowed:
            continue
        seen.add(release_id)
        releases.append(
            Release(
                id=release_id,
                title=str(release.get("name") or ""),
                type=release_type,
                year=_release_year(release),
            )
        )

    return artist_name, releases


async def list_playlist_tracks_async(
    url: str,
    *,
    client: Any = None,
) -> tuple[str, list]:
    """(playlist name, tracks) for a playlist subscription's URL.

    The whole listing on every check — a playlist has no "newest first" order
    to stop reading at, and tracks can be added anywhere in it. The tracks
    come back as the TrackMetadata the GUI's own fetch produces, so the new
    ones can be queued for download without looking each one up again.

    `client` is injectable so tests can drive this without Spotify.
    """
    import asyncio
    import inspect

    from .spotify_metadata import SpotifyMetadataClient, parse_spotify_url

    try:
        url_type = parse_spotify_url(url)["type"]
    except Exception as exc:
        raise SubscriptionError(f"{url} is not a Spotify playlist link") from exc
    if url_type != "playlist":
        msg = f"{url} is a {url_type}, not a playlist"
        raise SubscriptionError(msg)

    metadata_client = client or SpotifyMetadataClient()
    fetch = getattr(metadata_client, "get_url_async", None) or metadata_client.get_url
    if inspect.iscoroutinefunction(fetch):
        result = await fetch(url)
    else:
        result = await asyncio.to_thread(fetch, url)
    return str(result[0] or ""), list(result[1] or [])


def _track_release(track) -> Release:
    title = getattr(track, "title", "") or ""
    artists = getattr(track, "artists", "") or ""
    return Release(
        id=str(track.id),
        title=f"{title} — {artists}" if artists else title,
        type="track",
    )


def _release_year(release: dict) -> str:
    date = release.get("date")
    if isinstance(date, dict):
        year = date.get("year")
        if year:
            return str(year)
        iso = date.get("isoString") or ""
        return str(iso)[:4]
    if isinstance(date, str):
        return date[:4]
    return ""


@dataclass
class CheckResult:
    """What one check found. `new` is empty on a first (watermarking) check.

    For a playlist, `artist_name` is the playlist's name, `new` lists the
    added tracks and `new_tracks` carries their full TrackMetadata.
    """

    subscription: Subscription
    artist_name: str = ""
    total: int = 0
    new: list[Release] = field(default_factory=list)
    watermarked: bool = False
    error: str = ""
    new_tracks: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription.id,
            "url": self.subscription.url,
            "kind": self.subscription.kind,
            "artist": self.artist_name or self.subscription.name,
            "total_releases": self.total,
            "new_releases": [r.to_dict() for r in self.new],
            "watermarked": self.watermarked,
            "error": self.error,
        }


async def check_async(
    sub: Subscription,
    *,
    backfill: bool = False,
    client: Any = None,
) -> CheckResult:
    """Lists what is new for one subscription, and records it as seen.

    Marking happens here, not after a download, and that is deliberate: a
    release whose download fails must not be re-offered on every subsequent
    check forever. The failure is reported by the download path (and by
    `--json`), and `--subscribe-reset` is how you ask for another attempt.
    """
    tracks: list = []
    try:
        if sub.kind == "playlist":
            artist_name, tracks = await list_playlist_tracks_async(
                sub.url, client=client
            )
            tracks = [t for t in tracks if getattr(t, "id", "")]
            releases = list({str(t.id): _track_release(t) for t in tracks}.values())
        else:
            artist_name, releases = await list_releases_async(
                sub.url, sub.include_groups, client=client
            )
    except Exception as exc:
        record_check(sub.id, str(exc))
        return CheckResult(subscription=sub, error=str(exc))

    already = seen_ids(sub.id)
    first_check = not is_baselined(sub.id) and not already

    fresh = [r for r in releases if r.id not in already]
    mark_seen(sub.id, releases)
    record_check(sub.id, None)
    mark_baselined(sub.id)

    if artist_name and not sub.name:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE subscriptions SET name = ? WHERE id = ?", (artist_name, sub.id)
            )

    if first_check and not backfill:
        # See the module docstring: everything that exists today is the
        # baseline, not a backlog to fetch.
        return CheckResult(
            subscription=sub,
            artist_name=artist_name,
            total=len(releases),
            new=[],
            watermarked=True,
        )

    fresh_ids = {r.id for r in fresh}
    new_tracks: list = []
    for track in tracks:
        if str(track.id) in fresh_ids:
            new_tracks.append(track)
            fresh_ids.discard(str(track.id))  # a track listed twice, once

    return CheckResult(
        subscription=sub,
        artist_name=artist_name,
        total=len(releases),
        new=fresh,
        new_tracks=new_tracks,
    )


async def check_all_async(
    *,
    owner: str | None = None,
    backfill: bool = False,
    client: Any = None,
) -> list[CheckResult]:
    """Checks every enabled subscription, sequentially.

    Sequential on purpose: each check is several Spotify GraphQL calls, and
    running twenty artists at once is how an instance gets rate-limited.
    """
    results = []
    for sub in list_all(owner=owner, enabled_only=True):
        results.append(await check_async(sub, backfill=backfill, client=client))
    return results


async def sync_async(
    results: list[CheckResult],
    download: Callable[[str, str], Awaitable[None]],
) -> int:
    """Hands every new release's URL to `download(url, output_dir)`.

    The downloader is injected rather than imported so this module keeps no
    dependency on the download machinery — the CLI passes a closure around
    `_run_download_async`, and tests pass a recorder.

    Returns the number of releases dispatched. A release whose download
    raises is logged and skipped: one unavailable album must not stop the
    rest of the run.
    """
    dispatched = 0
    for result in results:
        for release in result.new:
            try:
                await download(release.url, result.subscription.output_dir)
                dispatched += 1
            except Exception:
                logger.exception(
                    "[subscriptions] Could not fetch %s (%s)",
                    release.title or release.id,
                    result.subscription.name or result.subscription.url,
                )
    return dispatched

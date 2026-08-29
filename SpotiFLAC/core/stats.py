"""core/stats.py — the download log, read back as a picture of a library.

`core/download_log.py` writes one row per finished track because three
features needed to *look one track up*: quotas count, subscriptions check,
deduplication asks "have I got this?". None of them ever read the table as a
whole.

Read as a whole, the same rows answer a different kind of question — the one
people actually ask about their own library. How much of it is there. Which
artists it is really made of, as opposed to which ones you would have named.
Which genres, which decades. When you do this: the month you went on a spree,
the hour of the day, the longest run of consecutive days. That is what this
module computes, and it is deliberately all it does: no formatting decisions
beyond `format_wrapped` for the CLI, no HTTP, no filesystem.

Everything comes from one query
-------------------------------
Aggregating in SQL would mean five round trips and still couldn't do the two
that matter most: `artist` holds "A, B" and `genre` holds "Rock; Alternative",
and splitting a string is not something SQLite does. So one `SELECT` over the
window feeds `collections.Counter`, which for a personal library — tens of
thousands of rows at the very outside — is a few tens of milliseconds and a
few megabytes.

What "unknown" means
--------------------
Genre, release year and duration arrived with schema v2 (see core/db.py).
Rows written before it have none, and rows written since only have a genre if
metadata enrichment was on. Every section that depends on them reports its
own coverage (`known` / `unknown`) rather than quietly presenting a fifth of
the library as all of it — a dashboard that overstates what it knows is worse
than one that admits a gap.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from . import db

logger = logging.getLogger(__name__)

#: How many entries each "top" list carries by default.
DEFAULT_TOP = 10

#: `artist` is stored as one string ("Daft Punk, Julian Casablancas") and
#: `genre` as another ("Rock; Alternative Rock"). Both are counted per name:
#: a feature credits both artists, and a track tagged with three genres is
#: three genres. Anything in this pattern separates them.
_SPLIT_RE = re.compile(r"\s*[;,/]\s*|\s+&\s+|\s+feat\.?\s+|\s+ft\.?\s+", re.IGNORECASE)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _split_names(value: str) -> list[str]:
    if not value:
        return []
    names = [part.strip() for part in _SPLIT_RE.split(value)]
    return [name for name in names if name and name.lower() != "unknown"]


def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _ranked(counter: Counter, total: int, limit: int, key: str = "name") -> list[dict]:
    return [
        {key: name, "tracks": count, "share": _share(count, total)}
        for name, count in counter.most_common(limit)
    ]


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """The period a dashboard covers, and how to say so."""

    since: float | None = None
    until: float | None = None
    label: str = "all time"

    def to_dict(self) -> dict:
        return {"since": self.since, "until": self.until, "label": self.label}


def year_window(year: int) -> Window:
    """A calendar year in local time — what "your 2026" means to the reader."""
    start = datetime(year, 1, 1).timestamp()
    end = datetime(year + 1, 1, 1).timestamp()
    return Window(since=start, until=end, label=str(year))


def days_window(days: int, *, now: float | None = None) -> Window:
    end = now if now is not None else time.time()
    return Window(
        since=end - days * 86400,
        until=None,
        label=f"last {days} day{'s' if days != 1 else ''}",
    )


def parse_window(
    *,
    year: int | None = None,
    days: int | None = None,
    since: float | None = None,
) -> Window:
    """The one place the CLI, the REST API and the GUI agree on a period."""
    if year is not None:
        return year_window(year)
    if days is not None:
        return days_window(days)
    if since is not None:
        return Window(since=since, label="since given date")
    return Window()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _fetch(window: Window, owner: str | None) -> list[Any]:
    sql = ["SELECT * FROM downloads WHERE 1=1"]
    params: list = []
    if owner is not None:
        sql.append("AND owner = ?")
        params.append(owner)
    if window.since is not None:
        sql.append("AND downloaded_at >= ?")
        params.append(window.since)
    if window.until is not None:
        sql.append("AND downloaded_at < ?")
        params.append(window.until)
    sql.append("ORDER BY downloaded_at ASC")
    try:
        return db.connection().execute(" ".join(sql), params).fetchall()
    except Exception:
        logger.debug("[stats] could not read the download log", exc_info=True)
        return []


def _value(row: Any, name: str, default: Any = "") -> Any:
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


def wrapped(
    *,
    owner: str | None = None,
    window: Window | None = None,
    top: int = DEFAULT_TOP,
) -> dict:
    """Everything the dashboard shows, from one pass over the log.

    `owner=None` covers the whole instance; passing an account name narrows
    it to that account, which is what multi-user mode wants — one person's
    dashboard is not a view of everybody's downloads.

    Never raises: a dashboard is a read-only view, and an unreadable database
    should leave the rest of the application working. An empty history comes
    back as a valid document with zeroes in it, so callers have one shape to
    render rather than two.
    """
    window = window or Window()
    rows = _fetch(window, owner)
    successful = [row for row in rows if _value(row, "success", 0)]

    document: dict[str, Any] = {
        "generated_at": time.time(),
        "owner": owner or "",
        "window": window.to_dict(),
        "totals": _totals(rows, successful),
        "top_artists": [],
        "top_albums": [],
        "top_genres": {"known": 0, "unknown": 0, "entries": []},
        "top_tracks": [],
        "providers": [],
        "formats": [],
        "decades": {"known": 0, "unknown": 0, "entries": []},
        "timeline": [],
        "activity": _empty_activity(),
        "first": None,
        "last": None,
    }
    if not successful:
        return document

    total = len(successful)

    artists: Counter = Counter()
    albums: Counter = Counter()
    tracks: Counter = Counter()
    genres: Counter = Counter()
    decades: Counter = Counter()
    providers: Counter = Counter()
    formats: Counter = Counter()
    genre_known = decade_known = 0

    for row in successful:
        for name in _split_names(_value(row, "artist")):
            artists[name] += 1

        album = _value(row, "album")
        album_artist = _split_names(_value(row, "artist"))
        if album and album.lower() != "unknown":
            albums[(album, album_artist[0] if album_artist else "")] += 1

        title = _value(row, "title")
        if title:
            tracks[(title, album_artist[0] if album_artist else "")] += 1

        row_genres = _split_names(_value(row, "genre"))
        if row_genres:
            genre_known += 1
            for name in row_genres:
                genres[name.title()] += 1

        year = str(_value(row, "release_year") or "")
        if len(year) == 4 and year.isdigit():
            decade_known += 1
            decades[f"{int(year) // 10 * 10}s"] += 1

        providers[_value(row, "provider") or "unknown"] += 1
        formats[(_value(row, "format") or "unknown").lower()] += 1

    document["top_artists"] = _ranked(artists, total, top)
    document["top_albums"] = [
        {
            "name": name,
            "artist": artist,
            "tracks": count,
            "share": _share(count, total),
        }
        for (name, artist), count in albums.most_common(top)
    ]
    # A track counted more than once is a track fetched more than once — a
    # re-download after a quality upgrade, or the same song from two
    # playlists. Worth showing precisely because it is not obvious.
    document["top_tracks"] = [
        {"name": name, "artist": artist, "tracks": count}
        for (name, artist), count in tracks.most_common(top)
        if count > 1
    ]
    document["top_genres"] = {
        "known": genre_known,
        "unknown": total - genre_known,
        "entries": _ranked(genres, genre_known, top),
    }
    document["decades"] = {
        "known": decade_known,
        "unknown": total - decade_known,
        "entries": [
            {"name": name, "tracks": count, "share": _share(count, decade_known)}
            for name, count in sorted(decades.items())
        ],
    }
    document["providers"] = _ranked(providers, total, top)
    document["formats"] = _ranked(formats, total, top)
    document["timeline"] = _timeline(successful)
    document["activity"] = _activity(successful)
    document["first"] = _milestone(successful[0])
    document["last"] = _milestone(successful[-1])
    return document


def _totals(rows: Sequence[Any], successful: Sequence[Any]) -> dict:
    total_bytes = sum(int(_value(row, "bytes", 0) or 0) for row in successful)
    listening_ms = sum(int(_value(row, "duration_ms", 0) or 0) for row in successful)
    timed = sum(1 for row in successful if int(_value(row, "duration_ms", 0) or 0))
    artists = {
        name for row in successful for name in _split_names(_value(row, "artist"))
    }
    albums = {
        _value(row, "album")
        for row in successful
        if _value(row, "album") and _value(row, "album").lower() != "unknown"
    }
    attempts = len(rows)
    return {
        "tracks": len(successful),
        "failed": attempts - len(successful),
        "attempts": attempts,
        "success_rate": _share(len(successful), attempts),
        "bytes": total_bytes,
        "artists": len(artists),
        "albums": len(albums),
        "listening_ms": listening_ms,
        # How much of the listening time is actually measured: durations are
        # only recorded from schema v2 onwards.
        "listening_known": timed,
    }


def _timeline(rows: Sequence[Any]) -> list[dict]:
    """Tracks and bytes per calendar month, with the empty months in between.

    A gap month is a fact about the history, and leaving it out of the series
    turns a two-month pause into a straight line between two peaks.
    """
    per_month: dict[str, dict] = {}
    for row in rows:
        stamp = datetime.fromtimestamp(float(_value(row, "downloaded_at", 0) or 0))
        key = f"{stamp.year:04d}-{stamp.month:02d}"
        entry = per_month.setdefault(key, {"month": key, "tracks": 0, "bytes": 0})
        entry["tracks"] += 1
        entry["bytes"] += int(_value(row, "bytes", 0) or 0)

    if not per_month:
        return []

    ordered = sorted(per_month)
    first_year, first_month = (int(part) for part in ordered[0].split("-"))
    last_year, last_month = (int(part) for part in ordered[-1].split("-"))

    series: list[dict] = []
    year, month = first_year, first_month
    while (year, month) <= (last_year, last_month):
        key = f"{year:04d}-{month:02d}"
        series.append(per_month.get(key, {"month": key, "tracks": 0, "bytes": 0}))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return series


def _empty_activity() -> dict:
    return {
        "by_weekday": [0] * 7,
        "by_hour": [0] * 24,
        "busiest_day": None,
        "active_days": 0,
        "longest_streak": 0,
        "current_streak": 0,
    }


def _activity(rows: Sequence[Any]) -> dict:
    """When downloading happens, in the reader's own timezone.

    Local time, deliberately: "you download most on Sunday evenings" is a
    statement about the person, and UTC would move it by a working day's
    worth of hours for anyone far enough east or west.
    """
    activity = _empty_activity()
    per_day: Counter = Counter()

    for row in rows:
        stamp = datetime.fromtimestamp(float(_value(row, "downloaded_at", 0) or 0))
        activity["by_weekday"][stamp.weekday()] += 1
        activity["by_hour"][stamp.hour] += 1
        per_day[stamp.date()] += 1

    if not per_day:
        return activity

    busiest, busiest_count = per_day.most_common(1)[0]
    activity["busiest_day"] = {"date": busiest.isoformat(), "tracks": busiest_count}
    activity["active_days"] = len(per_day)
    activity["longest_streak"], activity["current_streak"] = _streaks(per_day)
    return activity


def _streaks(per_day: Iterable[date]) -> tuple[int, int]:
    """Longest run of consecutive days, and the run still going.

    The current streak counts back from today, and tolerates a day that has
    not happened yet: a streak checked at nine in the morning has not been
    broken by not having downloaded anything since midnight.
    """
    days = sorted(set(per_day))
    if not days:
        return 0, 0

    longest = run = 1
    for previous, current in zip(days, days[1:]):
        run = run + 1 if current - previous == timedelta(days=1) else 1
        longest = max(longest, run)

    today = date.today()
    if days[-1] < today - timedelta(days=1):
        return longest, 0

    current_streak = 1
    for previous, following in zip(reversed(days[:-1]), reversed(days[1:])):
        if following - previous != timedelta(days=1):
            break
        current_streak += 1
    return longest, current_streak


def _milestone(row: Any) -> dict:
    return {
        "title": _value(row, "title"),
        "artist": _value(row, "artist"),
        "album": _value(row, "album"),
        "provider": _value(row, "provider"),
        "downloaded_at": float(_value(row, "downloaded_at", 0) or 0),
    }


# ---------------------------------------------------------------------------
# Rendering (CLI)
# ---------------------------------------------------------------------------


def human_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(milliseconds: float) -> str:
    seconds = int(milliseconds // 1000)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _bar(value: int, peak: int, width: int = 24) -> str:
    if peak <= 0:
        return ""
    filled = max(1, round(width * value / peak)) if value else 0
    return "█" * filled + "·" * (width - filled)


def _section(title: str, rows: list[tuple[str, int, str]]) -> list[str]:
    if not rows:
        return []
    peak = max(count for _label, count, _suffix in rows)
    width = min(28, max(len(label) for label, _count, _suffix in rows))
    lines = [f"  {title}"]
    for label, count, suffix in rows:
        lines.append(
            f"    {label[:width]:<{width}}  {_bar(count, peak)}  {count}{suffix}"
        )
    lines.append("")
    return lines


def format_wrapped(document: dict) -> str:
    """The dashboard as text, for `spotiflac --stats`.

    Same numbers as the GUI shows, in the place a headless install (a NAS, a
    container, an SSH session) can actually read them.
    """
    totals = document["totals"]
    if not totals["tracks"]:
        return (
            f"Nothing downloaded yet ({document['window']['label']}).\n"
            "The dashboard is built from the download log, which fills up as "
            "you fetch tracks."
        )

    lines = [
        f"SpotiFLAC — {document['window']['label']}"
        + (f" · {document['owner']}" if document["owner"] else ""),
        "",
        f"  {totals['tracks']} track(s) · {totals['artists']} artist(s) · "
        f"{totals['albums']} album(s) · {human_bytes(totals['bytes'])}",
    ]
    if totals["listening_known"]:
        lines.append(
            f"  {human_duration(totals['listening_ms'])} of music"
            + (
                ""
                if totals["listening_known"] == totals["tracks"]
                else f" (timed for {totals['listening_known']} of them)"
            )
        )
    if totals["failed"]:
        lines.append(
            f"  {totals['failed']} failed attempt(s) · "
            f"{totals['success_rate'] * 100:.0f}% success rate"
        )
    lines.append("")

    lines += _section(
        "Top artists",
        [(entry["name"], entry["tracks"], "") for entry in document["top_artists"]],
    )
    lines += _section(
        "Top albums",
        [
            (f"{entry['name']} — {entry['artist']}", entry["tracks"], "")
            for entry in document["top_albums"]
        ],
    )

    genres = document["top_genres"]
    if genres["entries"]:
        lines += _section(
            "Top genres"
            + (
                f"  (known for {genres['known']} of {totals['tracks']})"
                if genres["unknown"]
                else ""
            ),
            [(entry["name"], entry["tracks"], "") for entry in genres["entries"]],
        )

    decades = document["decades"]
    if decades["entries"]:
        lines += _section(
            "By decade",
            [(entry["name"], entry["tracks"], "") for entry in decades["entries"]],
        )

    lines += _section(
        "Providers",
        [(entry["name"], entry["tracks"], "") for entry in document["providers"]],
    )

    timeline = document["timeline"][-12:]
    if timeline:
        lines += _section(
            "Last months",
            [(entry["month"], entry["tracks"], "") for entry in timeline],
        )

    activity = document["activity"]
    if any(activity["by_weekday"]):
        lines += _section(
            "By weekday",
            [
                (WEEKDAYS[index], count, "")
                for index, count in enumerate(activity["by_weekday"])
            ],
        )

    if activity["busiest_day"]:
        lines.append(
            f"  Busiest day: {activity['busiest_day']['date']} "
            f"({activity['busiest_day']['tracks']} tracks)"
        )
    lines.append(
        f"  Active on {activity['active_days']} day(s) · "
        f"longest streak {activity['longest_streak']} day(s)"
        + (
            f" · {activity['current_streak']} day(s) running"
            if activity["current_streak"] > 1
            else ""
        )
    )

    if document["first"]:
        first = document["first"]
        stamp = datetime.fromtimestamp(first["downloaded_at"]).strftime("%Y-%m-%d")
        lines.append(
            f"  First in this period: {first['title']} — {first['artist']} ({stamp})"
        )

    return "\n".join(lines)

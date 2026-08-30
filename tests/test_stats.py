"""`--stats` — the download log read back as a dashboard (core/stats.py).

The log is written one row at a time by the post-download hook; these build
that table directly, with timestamps under the test's control, and check what
the dashboard makes of it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from SpotiFLAC.core import db, stats


def _add(
    *,
    title: str = "Song",
    artist: str = "Artist",
    album: str = "Album",
    provider: str = "ext:tidal-web",
    fmt: str = "flac",
    genre: str = "",
    release_year: str = "",
    duration_ms: int = 0,
    size_bytes: int = 1_000_000,
    success: bool = True,
    owner: str = "",
    at: float | None = None,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO downloads (
                owner, spotify_id, isrc, title, artist, album, provider,
                file_path, format, bytes, success, downloaded_at,
                genre, release_year, duration_ms
            ) VALUES (?, '', '', ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner,
                title,
                artist,
                album,
                provider,
                fmt,
                size_bytes,
                1 if success else 0,
                at if at is not None else time.time(),
                genre,
                release_year,
                duration_ms,
            ),
        )


def _at(days_ago: float, hour: int = 12) -> float:
    """A local-time timestamp, since that is what the dashboard reports in."""
    moment = datetime.now().replace(
        hour=hour, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_ago)
    return moment.timestamp()


def test_an_empty_history_is_a_valid_document_not_an_error() -> None:
    document = stats.wrapped()

    assert document["totals"]["tracks"] == 0
    assert document["top_artists"] == []
    assert document["timeline"] == []
    assert document["activity"]["busiest_day"] is None
    # One shape to render, whether or not anything has been downloaded.
    assert "Nothing downloaded yet" in stats.format_wrapped(document)


def test_totals_count_what_landed_and_what_only_tried() -> None:
    _add(title="A", artist="Queen", album="Opera", size_bytes=2_000_000)
    _add(title="B", artist="Queen", album="Opera", size_bytes=3_000_000)
    _add(title="C", artist="Queen", album="Opera", success=False)

    totals = stats.wrapped()["totals"]

    assert totals["tracks"] == 2
    assert totals["failed"] == 1
    assert totals["attempts"] == 3
    assert totals["bytes"] == 5_000_000
    assert totals["artists"] == 1
    assert totals["albums"] == 1
    assert totals["success_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_every_credited_artist_is_counted_not_just_the_first() -> None:
    _add(title="Instant Crush", artist="Daft Punk, Julian Casablancas")
    _add(title="Get Lucky", artist="Daft Punk")

    names = {entry["name"]: entry["tracks"] for entry in stats.wrapped()["top_artists"]}

    assert names == {"Daft Punk": 2, "Julian Casablancas": 1}


def test_a_track_tagged_with_several_genres_counts_for_each() -> None:
    _add(genre="Rock; Alternative Rock")
    _add(genre="Rock")
    # Nothing enriched this one: it is not a genre called "".
    _add(genre="")

    genres = stats.wrapped()["top_genres"]

    assert genres["known"] == 2
    assert genres["unknown"] == 1
    assert genres["entries"][0] == {"name": "Rock", "tracks": 2, "share": 1.0}


def test_the_dashboard_says_how_much_of_the_history_predates_the_columns() -> None:
    # Rows written before schema v2 have no genre, year or duration at all.
    for _ in range(3):
        _add(genre="", release_year="", duration_ms=0)
    _add(genre="Jazz", release_year="1959", duration_ms=300_000)

    document = stats.wrapped()

    assert document["top_genres"] == {
        "known": 1,
        "unknown": 3,
        "entries": [{"name": "Jazz", "tracks": 1, "share": 1.0}],
    }
    assert document["decades"]["known"] == 1
    assert document["decades"]["entries"] == [
        {"name": "1950s", "tracks": 1, "share": 1.0}
    ]
    assert document["totals"]["listening_known"] == 1
    assert document["totals"]["listening_ms"] == 300_000


def test_the_timeline_keeps_the_months_nothing_happened_in() -> None:
    _add(at=datetime(2026, 1, 15, 12).timestamp())
    _add(at=datetime(2026, 4, 2, 12).timestamp())

    timeline = stats.wrapped()["timeline"]

    # A three-month pause is a fact about the history; dropping the empty
    # months would draw a straight line between the two peaks.
    assert [entry["month"] for entry in timeline] == [
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
    ]
    assert [entry["tracks"] for entry in timeline] == [1, 0, 0, 1]


def test_activity_reports_the_busiest_day_and_the_streak() -> None:
    _add(at=_at(2, hour=9))
    _add(at=_at(1, hour=23))
    _add(at=_at(1, hour=23))
    _add(at=_at(0, hour=23))

    activity = stats.wrapped()["activity"]

    assert activity["active_days"] == 3
    assert activity["busiest_day"]["tracks"] == 2
    assert activity["longest_streak"] == 3
    assert activity["current_streak"] == 3
    assert activity["by_hour"][23] == 3


def test_a_streak_that_ended_a_week_ago_is_not_still_running() -> None:
    _add(at=_at(9))
    _add(at=_at(8))

    activity = stats.wrapped()["activity"]

    assert activity["longest_streak"] == 2
    assert activity["current_streak"] == 0


def test_a_window_covers_one_period_and_says_which() -> None:
    _add(title="Old", at=datetime(2024, 6, 1, 12).timestamp())
    _add(title="New", at=datetime(2026, 6, 1, 12).timestamp())

    document = stats.wrapped(window=stats.year_window(2026))

    assert document["window"]["label"] == "2026"
    assert document["totals"]["tracks"] == 1
    assert document["first"]["title"] == "New"


def test_one_account_sees_its_own_downloads_not_everybody_s() -> None:
    _add(title="Mine", owner="ada")
    _add(title="Theirs", owner="grace")

    mine = stats.wrapped(owner="ada")
    everyone = stats.wrapped()

    assert mine["totals"]["tracks"] == 1
    assert mine["first"]["title"] == "Mine"
    assert everyone["totals"]["tracks"] == 2


def test_a_track_fetched_twice_is_surfaced_and_a_single_one_is_not() -> None:
    _add(title="Everlong", artist="Foo Fighters")
    _add(title="Everlong", artist="Foo Fighters")
    _add(title="Monkey Wrench", artist="Foo Fighters")

    top_tracks = stats.wrapped()["top_tracks"]

    assert top_tracks == [
        {"name": "Everlong", "artist": "Foo Fighters", "tracks": 2},
    ]


def test_the_text_rendering_carries_the_numbers_it_summarises() -> None:
    _add(
        artist="Queen", album="A Night at the Opera", genre="Rock", size_bytes=4_000_000
    )
    _add(
        artist="Queen", album="A Night at the Opera", genre="Rock", size_bytes=4_000_000
    )

    rendered = stats.format_wrapped(stats.wrapped())

    assert "2 track(s)" in rendered
    assert "Queen" in rendered
    assert "Rock" in rendered


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [(0, "0m"), (90_000, "1m"), (3_600_000, "1h 0m"), (90_000_000, "1d 1h")],
)
def test_listening_time_is_readable_at_every_scale(
    milliseconds: int, expected: str
) -> None:
    assert stats.human_duration(milliseconds) == expected

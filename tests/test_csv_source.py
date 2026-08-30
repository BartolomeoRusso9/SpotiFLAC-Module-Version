"""`--csv` — a CSV file as an input source (see core/csv_source.py).

The parsing half is exercised against the real shapes people arrive with
(Exportify, a semicolon-separated European export, a bare list of links),
because that is where the awkward cases are. The resolution half is
exercised against a fake catalogue: what matters is which row is accepted,
which is reported, and that neither ever reaches the network on its own.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import csv_source
from SpotiFLAC.core.errors import SpotiflacError
from SpotiFLAC.core.models import TrackMetadata

EXPORTIFY = (
    '"Track URI","Track Name","Artist Name(s)","Album Name","ISRC","Duration (ms)"\n'
    '"spotify:track:4uLU6hMCjMI75M1A2tKUQC","Never Gonna Give You Up",'
    '"Rick Astley","Whenever You Need Somebody","GBARL9300135","213573"\n'
    '"spotify:track:1301WleyT98MSxVHPZCA6M","Everlong","Foo Fighters",'
    '"The Colour and the Shape","USRW29600011","250546"\n'
)


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _track(
    track_id: str = "abc",
    title: str = "Everlong",
    artists: str = "Foo Fighters",
    album: str = "The Colour and the Shape",
    duration_ms: int = 250000,
) -> TrackMetadata:
    return TrackMetadata(
        id=track_id,
        title=title,
        artists=artists,
        album=album,
        album_artist=artists,
        duration_ms=duration_ms,
        external_url=f"https://open.spotify.com/track/{track_id}",
    )


class _FakeCatalogue:
    """Stands in for SpotifyMetadataClient: only `search_tracks_async` is used."""

    def __init__(self, results: list | None = None, error: Exception | None = None):
        self.results = results or []
        self.error = error
        self.queries: list[str] = []

    async def search_tracks_async(self, query: str, limit: int = 8) -> list:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.results


class _FakeResolver:
    def __init__(self, url: str = "") -> None:
        self.url = url
        self.asked: list[str] = []

    async def spotify_url_for_isrc_async(self, isrc: str) -> str:
        self.asked.append(isrc)
        return self.url


# ── Parsing ───────────────────────────────────────────────────────────────


def test_an_exportify_export_is_read_by_its_column_names(tmp_path) -> None:
    document = csv_source.read_rows(_write(tmp_path, "playlist.csv", EXPORTIFY))

    assert document.has_header
    assert document.columns["title"] == "Track Name"
    assert document.columns["artist"] == "Artist Name(s)"
    assert len(document.rows) == 2

    first = document.rows[0]
    # The "Track URI" column is a spotify: URI, not something the downloader
    # could be handed as-is.
    assert first.url == "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
    assert first.isrc == "GBARL9300135"
    assert first.duration_ms == 213573


def test_the_delimiter_and_localised_headers_are_detected(tmp_path) -> None:
    text = (
        "Titolo;Artista;Album\n"
        "Everlong;Foo Fighters;The Colour and the Shape\n"
        "Bohemian Rhapsody;Queen;A Night at the Opera\n"
    )
    document = csv_source.read_rows(_write(tmp_path, "lista.csv", text))

    assert document.delimiter == ";"
    assert document.columns == {
        "title": "Titolo",
        "artist": "Artista",
        "album": "Album",
    }
    assert [row.artist for row in document.rows] == ["Foo Fighters", "Queen"]


def test_a_bare_list_of_links_has_no_header_and_keeps_its_first_line(
    tmp_path,
) -> None:
    text = (
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC\n"
        "spotify:track:1301WleyT98MSxVHPZCA6M\n"
        "\n"
        "https://open.spotify.com/album/6QaVfG1pHYl1z15ZxkvVDW\n"
    )
    document = csv_source.read_rows(_write(tmp_path, "links.csv", text))

    assert not document.has_header
    assert len(document.rows) == 3
    assert document.rows[0].url.endswith("/track/4uLU6hMCjMI75M1A2tKUQC")
    # A blank line is skipped without shifting the line numbers of what follows.
    assert document.rows[2].line == 4
    assert "/album/" in document.rows[2].url


def test_headerless_rows_are_read_by_what_their_values_look_like(tmp_path) -> None:
    text = (
        "GBARL9300135,Never Gonna Give You Up,Rick Astley\nQueen - Bohemian Rhapsody\n"
    )
    document = csv_source.read_rows(_write(tmp_path, "mixed.csv", text))

    first, second = document.rows
    assert first.isrc == "GBARL9300135"
    assert (first.title, first.artist) == ("Never Gonna Give You Up", "Rick Astley")
    # "Artist - Title" is the convention for a single free-text cell.
    assert (second.artist, second.title) == ("Queen", "Bohemian Rhapsody")


def test_a_file_with_nothing_usable_in_it_is_an_error(tmp_path) -> None:
    with pytest.raises(SpotiflacError):
        csv_source.read_rows(_write(tmp_path, "empty.csv", "\n\n"))

    # A recognised header and no rows under it: the file is understood and
    # holds nothing, which is a mistake worth stopping for rather than a run
    # that downloads zero tracks and reports success.
    header_only = '"Track URI","Track Name","Artist Name(s)"\n'
    with pytest.raises(SpotiflacError):
        csv_source.read_rows(_write(tmp_path, "header-only.csv", header_only))


def test_a_missing_file_is_an_error_not_a_traceback(tmp_path) -> None:
    with pytest.raises(SpotiflacError):
        csv_source.read_rows(str(tmp_path / "nope.csv"))


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("213573", 213573),
        ("221", 221000),
        ("3:41", 221000),
        ("", 0),
        ("n/a", 0),
    ],
)
def test_durations_are_read_in_every_shape_exporters_write_them(
    written: str, expected: int
) -> None:
    assert csv_source._to_int(written) == expected


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (
            "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        ),
        (
            "4uLU6hMCjMI75M1A2tKUQC",
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        ),
        ("https://tidal.com/browse/track/1234", "https://tidal.com/browse/track/1234"),
        ("not a link", ""),
    ],
)
def test_every_way_of_writing_a_link_ends_up_a_url(written: str, expected: str) -> None:
    assert csv_source.normalize_link(written) == expected


# ── Matching ──────────────────────────────────────────────────────────────


def test_a_remaster_suffix_does_not_cost_a_match() -> None:
    row = csv_source.CsvRow(line=1, title="Everlong", artist="Foo Fighters")
    score = csv_source.match_score(row, _track(title="Everlong - Remastered 2011"))
    assert score >= csv_source.DEFAULT_MIN_SCORE


def test_a_different_song_by_the_same_artist_is_not_a_match() -> None:
    row = csv_source.CsvRow(line=1, title="Everlong", artist="Foo Fighters")
    score = csv_source.match_score(row, _track(title="Monkey Wrench"))
    assert score < csv_source.DEFAULT_MIN_SCORE


def test_a_wildly_different_running_time_pushes_a_candidate_down() -> None:
    row = csv_source.CsvRow(
        line=1, title="Everlong", artist="Foo Fighters", duration_ms=250000
    )
    same = csv_source.match_score(row, _track(duration_ms=250500))
    # The same title and artist, but eleven minutes long: a live version or a
    # DJ mix, not the recording the row meant.
    other = csv_source.match_score(row, _track(duration_ms=660000))
    assert other < same


# ── Resolution ────────────────────────────────────────────────────────────


def test_rows_that_already_carry_a_link_never_reach_the_catalogue() -> None:
    rows = [
        csv_source.CsvRow(line=1, url="https://open.spotify.com/track/aaa"),
        csv_source.CsvRow(line=2, url="https://open.spotify.com/track/bbb"),
    ]
    catalogue = _FakeCatalogue()

    resolution = asyncio.run(csv_source.resolve_rows(rows, client=catalogue))

    assert catalogue.queries == []
    assert [entry.how for entry in resolution.resolved] == ["link", "link"]
    assert resolution.unresolved == ()


def test_a_text_row_is_matched_and_a_hopeless_one_is_reported() -> None:
    rows = [
        csv_source.CsvRow(line=2, title="Everlong", artist="Foo Fighters"),
        csv_source.CsvRow(line=3, title="Nothing Like This", artist="Nobody At All"),
    ]
    catalogue = _FakeCatalogue([_track("aaa"), _track("bbb", title="Monkey Wrench")])

    resolution = asyncio.run(csv_source.resolve_rows(rows, client=catalogue))

    (matched,) = resolution.resolved
    assert matched.url == "https://open.spotify.com/track/aaa"
    assert matched.how == "search"

    (missed,) = resolution.unresolved
    assert missed.row.line == 3
    # The report says what the closest thing was, so the user can see whether
    # the file is wrong or the threshold is.
    assert "Everlong" in missed.best or "Monkey Wrench" in missed.best


def test_an_isrc_row_resolves_through_spotifys_own_catalogue() -> None:
    """A row identified only by its ISRC must not depend on Songlink.

    Odesli retired free public access to the v1-alpha.1 API — every request
    now answers 401 PUBLIC_API_ACCESS_DEPRECATED — and the failure was
    silent: the row came back "ISRC not found", which is precisely the row
    that could otherwise have been matched with certainty rather than
    guessed at. Spotify's own `isrc:` search operator answers it instead.
    """
    track = _track(title="Window Shopper", artists="50 Cent")
    catalogue = _FakeCatalogue([track])
    rows = [csv_source.CsvRow(line=2, isrc="USUM70504267")]

    # No resolver at all: if the implementation still needed one, this would
    # reach for the real (dead) LinkResolver instead of the catalogue.
    resolution = asyncio.run(csv_source.resolve_rows(rows, client=catalogue))

    assert len(resolution.resolved) == 1
    resolved = resolution.resolved[0]
    assert resolved.how == "isrc"
    assert catalogue.queries == ["isrc:USUM70504267"], (
        "the ISRC has to be sent as an isrc: query — a bare ISRC is scored "
        "as free text by Spotify and returns unrelated tracks"
    )


def test_an_isrc_is_the_second_chance_when_the_text_does_not_match() -> None:
    rows = [
        csv_source.CsvRow(
            line=2,
            title="Titolo Tradotto",
            artist="Interprete",
            isrc="GBARL9300135",
        )
    ]
    resolver = _FakeResolver("https://open.spotify.com/track/from-isrc")

    resolution = asyncio.run(
        csv_source.resolve_rows(rows, client=_FakeCatalogue([]), resolver=resolver)
    )

    (entry,) = resolution.resolved
    assert entry.how == "isrc"
    assert entry.url.endswith("from-isrc")
    assert resolver.asked == ["GBARL9300135"]


def test_an_isrc_only_row_that_nothing_knows_about_is_reported() -> None:
    rows = [csv_source.CsvRow(line=2, isrc="GBARL9300135")]

    resolution = asyncio.run(
        csv_source.resolve_rows(rows, client=_FakeCatalogue(), resolver=_FakeResolver())
    )

    assert resolution.resolved == ()
    assert resolution.unresolved[0].reason == "ISRC not found"


def test_one_failing_lookup_does_not_take_the_file_down_with_it() -> None:
    rows = [
        csv_source.CsvRow(line=2, title="Everlong", artist="Foo Fighters"),
        csv_source.CsvRow(line=3, url="https://open.spotify.com/track/aaa"),
    ]
    catalogue = _FakeCatalogue(error=RuntimeError("catalogue is down"))

    resolution = asyncio.run(csv_source.resolve_rows(rows, client=catalogue))

    # The link row still resolves; the searched one is reported, not raised.
    assert [entry.row.line for entry in resolution.resolved] == [3]
    assert resolution.unresolved[0].row.line == 2


def test_the_same_track_listed_twice_is_downloaded_once() -> None:
    rows = [
        csv_source.CsvRow(line=1, url="https://open.spotify.com/track/aaa"),
        csv_source.CsvRow(line=2, url="https://open.spotify.com/track/aaa"),
        csv_source.CsvRow(line=3, url="https://open.spotify.com/track/bbb"),
    ]

    resolution = asyncio.run(csv_source.resolve_rows(rows))

    assert resolution.urls == [
        "https://open.spotify.com/track/aaa",
        "https://open.spotify.com/track/bbb",
    ]


def test_the_unresolved_report_can_be_corrected_and_fed_back_in(tmp_path) -> None:
    unresolved = [
        csv_source.UnresolvedRow(
            row=csv_source.CsvRow(line=7, title="Everlong", artist="Foo Fighters"),
            reason="no match above 0.62",
            best="Monkey Wrench — Foo Fighters",
            score=0.31,
        )
    ]
    target = tmp_path / "unmatched.csv"

    assert csv_source.write_unresolved(target, unresolved) == 1

    # The point of the file: it is itself a valid --csv input.
    document = csv_source.read_rows(str(target))
    assert document.rows[0].title == "Everlong"
    assert document.rows[0].artist == "Foo Fighters"

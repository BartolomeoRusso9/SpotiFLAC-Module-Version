"""Catching a provider that resolved the wrong recording.

core/download_validation.py already proves a downloaded file is a whole
track rather than a preview. It cannot prove it is *this* track: a cover, a
re-recording, a live take or a different artist's song of the same name all
run roughly the right length. Without the check these tests cover, such a
file is saved and then tagged with the requested track's metadata — the
audio of one song carrying another song's name, permanently.

checkAvailability() returns only {available, track_id} in every extension
shipped today, so nothing can be verified before the download. download()
does report the title/artist/album it fetched, which is where this check
gets its evidence.

The asymmetry throughout is deliberate: only positive disagreement rejects.
An extension that reports nothing about what it fetched cannot be
contradicted, and treating silence as a mismatch would break every provider
that stays quiet.
"""

from __future__ import annotations

import pytest

from SpotiFLAC.core.text_match import titles_match, track_identity_mismatch


def _check(**kwargs) -> str:
    return track_identity_mismatch(**kwargs)


# --- the wrong track must be caught ----------------------------------------


def test_a_different_artists_cover_is_rejected() -> None:
    reason = _check(
        expected_title="Hurt",
        expected_artist="Nine Inch Nails",
        found_title="Hurt",
        found_artist="Johnny Cash",
    )
    assert "different artist" in reason


def test_a_different_song_by_the_same_artist_is_rejected() -> None:
    reason = _check(
        expected_title="Can't Stop",
        expected_artist="Red Hot Chili Peppers",
        found_title="Don't Stop",
        found_artist="Red Hot Chili Peppers",
    )
    assert "different title" in reason


def test_a_conflicting_isrc_settles_it_outright() -> None:
    """An ISRC names the recording. Two different ones cannot be the same
    track, whatever the titles say.
    """
    reason = _check(
        expected_title="Window Shopper",
        expected_artist="50 Cent",
        expected_isrc="USUM70504267",
        found_title="Window Shopper",
        found_artist="50 Cent",
        found_isrc="USIR10300033",
    )
    assert "different recording" in reason


# --- the right track must not be rejected ----------------------------------


def test_a_matching_isrc_overrides_every_text_disagreement() -> None:
    """The identity is established; the catalogues may render the names
    however they like.
    """
    assert (
        _check(
            expected_title="Window Shopper",
            expected_artist="50 Cent",
            expected_isrc="USUM70504267",
            found_title="WINDOW SHOPPER (Explicit)",
            found_artist="Curtis Jackson",
            found_isrc="USUM70504267",
        )
        == ""
    )


@pytest.mark.parametrize(
    ("expected_title", "found_title", "why"),
    [
        ("Everlong", "Everlong (Remastered)", "a remaster suffix"),
        ("Sicko Mode", "SICKO MODE", "case"),
        ("Cafe", "Café", "an accent the file lost"),
        ("Where Is My Mind?", "Where Is My Mind", "punctuation"),
        ("Juicy", "Juicy (feat. Total)", "a featured credit in the title"),
    ],
)
def test_the_same_song_titled_differently_is_accepted(
    expected_title, found_title, why
) -> None:
    assert (
        _check(
            expected_title=expected_title,
            expected_artist="Some Artist",
            found_title=found_title,
            found_artist="Some Artist",
        )
        == ""
    ), why


def test_a_featured_artist_in_the_credit_is_accepted() -> None:
    assert (
        _check(
            expected_title="Juicy",
            expected_artist="The Notorious B.I.G.",
            found_title="Juicy",
            found_artist="The Notorious B.I.G. feat. Total",
        )
        == ""
    )


def test_an_album_disagreement_alone_does_not_reject() -> None:
    """One recording appears on a single, an album, a deluxe edition and
    three compilations. Rejecting on the album name would fail constantly.
    """
    assert (
        _check(
            expected_title="Bohemian Rhapsody",
            expected_artist="Queen",
            expected_album="A Night at the Opera",
            found_title="Bohemian Rhapsody",
            found_artist="Queen",
            found_album="Bohemian Rhapsody (The Original Soundtrack)",
        )
        == ""
    )


def test_an_album_disagreement_does_reject_without_a_strong_identity() -> None:
    """The leniency above is bought by title and artist agreeing exactly.
    Where the title only nearly agrees, the album is evidence again.

    The decoration here is a remaster on purpose: a live or karaoke marker
    would be caught earlier, by the variant check, and this test would stop
    exercising the album rule it is named after.
    """
    reason = _check(
        expected_title="Alive",
        expected_artist="Pearl Jam",
        expected_album="Ten",
        found_title="Alive (2011 Remaster)",
        found_artist="Pearl Jam",
        found_album="Rearviewmirror: Greatest Hits",
    )
    assert "different album" in reason


# --- silence is never a mismatch -------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_title": "Juicy", "expected_artist": "Biggie"},
        {"found_title": "Juicy", "found_artist": "Biggie"},
        {"expected_title": "Juicy", "found_artist": "Someone Else"},
        {},
    ],
)
def test_a_provider_that_reports_nothing_is_not_contradicted(kwargs) -> None:
    """Every extension shipped today returns only {available, track_id} from
    checkAvailability, and several report little from download() either.
    Rejecting on absent information would break all of them.
    """
    assert _check(**kwargs) == ""


def test_titles_match_is_not_a_blanket_yes() -> None:
    assert titles_match("Everlong", "Everlong (Remastered)")
    assert not titles_match("Can't Stop", "Don't Stop")
    assert not titles_match("Time", "Money")

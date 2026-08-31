"""The lead artist of a credit whose own name contains a comma.

"Like Him" landed in an artist folder called "Tyler", because the credit
"Tyler, The Creator, Lola Young" was split on the comma and the first piece
taken. The join that produced that string is lossy — nothing in it says
whether the first comma separates two artists or sits inside one name — so
the fix is to keep the list the source actually had.
"""

from __future__ import annotations

import pytest

from SpotiFLAC.core.models import TrackMetadata, build_filename, split_credit

CHROMAKOPIA = {
    "id": "6jbYpRPTEFl1HFKHk1IC0m",
    "title": "Like Him (feat. Lola Young)",
    "artists": "Tyler, The Creator, Lola Young",
    "album": "CHROMAKOPIA",
    "album_artist": "Tyler, The Creator",
}


def test_the_lead_artist_keeps_the_comma_in_its_own_name() -> None:
    track = TrackMetadata(
        **CHROMAKOPIA,
        artist_names=["Tyler, The Creator", "Lola Young"],
        album_artist_names=["Tyler, The Creator"],
    )
    assert track.first_artist == "Tyler, The Creator"
    assert track.first_album_artist == "Tyler, The Creator"


def test_the_album_artist_lead_is_read_from_its_own_list() -> None:
    """The two credits differ on a featured track — taking the lead of the
    track credit for the album artist would keep a feature that the album
    credit does not name.
    """
    track = TrackMetadata(
        id="x",
        title="Girls Want Girls",
        artists="Drake, Lil Baby",
        album="Certified Lover Boy",
        album_artist="Drake",
        artist_names=["Drake", "Lil Baby"],
        album_artist_names=["Drake"],
    )
    assert track.first_artist == "Drake"
    assert track.first_album_artist == "Drake"
    assert track.as_flac_tags(first_artist_only=True)["ALBUMARTIST"] == "Drake"


def test_a_source_without_the_list_still_splits() -> None:
    """CSV rows and local files only ever have the joined string. Guessing
    is worse than the list and better than nothing.
    """
    track = TrackMetadata(
        id="x",
        title="Spin Bout U",
        artists="Drake, 21 Savage",
        album="Her Loss",
        album_artist="Drake, 21 Savage",
    )
    assert track.first_artist == "Drake"


def test_blank_entries_never_become_the_lead() -> None:
    track = TrackMetadata(**CHROMAKOPIA, artist_names=["", "  ", "Lola Young"])
    assert track.first_artist == "Lola Young"


def test_the_filename_follows_the_same_lead() -> None:
    track = TrackMetadata(
        **CHROMAKOPIA,
        artist_names=["Tyler, The Creator", "Lola Young"],
        album_artist_names=["Tyler, The Creator"],
    )
    name = build_filename(track, "{title} - {artist}", first_artist_only=True)
    assert name == "Like Him (feat. Lola Young) - Tyler, The Creator.flac"


# --- the multi-value tag writers --------------------------------------------


@pytest.mark.parametrize(
    ("joined", "known", "expected"),
    [
        # The case that broke: ARTIST was written as two values, "Tyler" and
        # "The Creator".
        (
            "Tyler, The Creator, Lola Young",
            ["Tyler, The Creator", "Lola Young"],
            ["Tyler, The Creator", "Lola Young"],
        ),
        ("Tyler, The Creator", ["Tyler, The Creator"], ["Tyler, The Creator"]),
        # A name in the list plus one that is not — the leftover still splits.
        ("Drake, Lil Baby", ["Drake"], ["Drake", "Lil Baby"]),
        # No list at all: the comma is all there is to go on.
        ("Drake, 21 Savage", None, ["Drake", "21 Savage"]),
        ("Sade", None, ["Sade"]),
        ("", ["Anyone"], []),
        # A known name must not be claimed by a shorter one that prefixes it.
        (
            "Tyler, The Creator",
            ["Tyler", "Tyler, The Creator"],
            ["Tyler, The Creator"],
        ),
    ],
)
def test_split_credit(joined, known, expected) -> None:
    assert split_credit(joined, known) == expected

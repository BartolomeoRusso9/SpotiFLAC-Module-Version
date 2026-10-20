"""A Spotify ID is exactly 22 base62 characters. A longer or shorter "id"
cannot go anywhere useful: GraphQL answers GenericError and the native
metadata endpoint overflows converting it, so the wrong shape must be
rejected at parse time with the same InvalidUrlError the router already
raises for non-URL junk.

Observed in the field (2026-10-20): a 23-character "track id"
(7qiZRh2vBY3q6ZFX6iW0qLb) surfaced from a link resolver and travelled all
the way to getTrack.
"""

from __future__ import annotations

import pytest

from mornye.core.errors import InvalidUrlError
from mornye.core.spotify_metadata import parse_spotify_url


def test_a_23_character_id_is_rejected() -> None:
    with pytest.raises(InvalidUrlError):
        parse_spotify_url("https://open.spotify.com/track/7qiZRh2vBY3q6ZFX6iW0qLb")


def test_a_21_character_id_is_rejected() -> None:
    with pytest.raises(InvalidUrlError):
        parse_spotify_url("https://open.spotify.com/track/short0123456789ab")


def test_a_valid_22_character_id_still_parses() -> None:
    assert parse_spotify_url(
        "https://open.spotify.com/track/2NHkwSwm6C6eAX3z6xm7Uy"
    ) == {
        "type": "track",
        "id": "2NHkwSwm6C6eAX3z6xm7Uy",
    }


def test_nested_playlist_ids_face_the_same_gate() -> None:
    """CodeRabbit finding: /user/<uid>/playlist/<id> returned its id unchecked."""
    with pytest.raises(InvalidUrlError):
        parse_spotify_url(
            "https://open.spotify.com/user/someone/playlist/7qiZRh2vBY3q6ZFX6iW0qLb"
        )


def test_artist_ids_face_the_same_gate() -> None:
    with pytest.raises(InvalidUrlError):
        parse_spotify_url("https://open.spotify.com/artist/7qiZRh2vBY3q6ZFX6iW0qLb")
    assert (
        parse_spotify_url("https://open.spotify.com/artist/2exebQUDoIoT0dXA8BcN1P")[
            "id"
        ]
        == "2exebQUDoIoT0dXA8BcN1P"
    )

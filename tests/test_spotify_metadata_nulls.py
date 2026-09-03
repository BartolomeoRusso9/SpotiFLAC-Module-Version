"""What Spotify sends for a track the account cannot see.

The shape comes back intact and every field is emptied — `"name": ""`,
`"duration": {"totalMilliseconds": 0}`, and, the one that mattered,
`"date": null` inside `albumOfTrack`. `.get("date", {})` only defaults when
the key is *absent*, so the chain reached `None.get("isoString")` and the
whole lookup died as "NETWORK_ERROR: Metadata fetch failed: 'NoneType'
object has no attribute 'get'". In a 1875-row CSV import that was one line
of noise in the log and one track missing from the table, with nothing
connecting the two.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.errors import ErrorKind, SpotiflacError
from SpotiFLAC.core.spotify_metadata import SpotifyMetadataClient


class _FakeWebClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def query(self, _payload: dict) -> dict:
        return self.payload

    def get_isrc_from_metadata(self, _track_id: str) -> str:
        return ""

    def get_track_composer(self, _track_id: str) -> str:
        return ""

    def extract_cover_url(self, art) -> str:
        assert art is not None, "a null node must never reach the cover reader"
        return ""


def _client(payload: dict, monkeypatch) -> SpotifyMetadataClient:
    client = SpotifyMetadataClient.__new__(SpotifyMetadataClient)
    client.web_client = _FakeWebClient(payload)  # type: ignore[attr-defined]

    async def _to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("SpotiFLAC.core.spotify_metadata.asyncio.to_thread", _to_thread)
    return client


def test_a_null_album_date_does_not_kill_the_lookup(monkeypatch) -> None:
    """The exact shape from the field, minus the unplayable flag: a real
    track whose album carries no date is still a track.
    """
    client = _client(
        {
            "data": {
                "trackUnion": {
                    "name": "Higuain",
                    "firstArtist": {"items": [{"profile": {"name": "Enzo Dong"}}]},
                    "albumOfTrack": {
                        "name": "Higuain",
                        "date": None,
                        "copyright": None,
                        "coverArt": None,
                        "artists": None,
                    },
                    "contentRating": None,
                    "duration": {"totalMilliseconds": 180_000},
                }
            }
        },
        monkeypatch,
    )

    track = asyncio.run(client.get_track_async("0" * 22))

    assert track.title == "Higuain"
    assert track.release_date == ""
    assert track.copyright == ""
    assert track.is_explicit is False


def test_an_unavailable_track_says_so_instead_of_arriving_untitled(monkeypatch) -> None:
    """Parsing the emptied payload would put a row titled "Unknown" in the
    track table that nothing can download. Naming the track and the reason
    is the only useful thing to do with it.
    """
    client = _client(
        {
            "data": {
                "trackUnion": {
                    "name": "",
                    "playability": {"playable": False, "reason": "COUNTRY_RESTRICTED"},
                    "albumOfTrack": {"name": "", "date": None},
                    "duration": {"totalMilliseconds": 0},
                }
            }
        },
        monkeypatch,
    )

    with pytest.raises(SpotiflacError) as excinfo:
        asyncio.run(client.get_track_async("0NfjJ4sWSQJl2SOIQiEQId"))

    assert excinfo.value.kind is ErrorKind.UNAVAILABLE
    assert "0NfjJ4sWSQJl2SOIQiEQId" in excinfo.value.message
    assert "country restricted" in excinfo.value.message


def test_a_failed_query_is_reported_as_a_missing_track(monkeypatch) -> None:
    """GraphQL answers a partly failed query with `{"data": null}`. That is
    a track that could not be read, not a track named "Unknown".
    """
    client = _client({"data": None, "errors": [{"message": "boom"}]}, monkeypatch)

    with pytest.raises(SpotiflacError) as excinfo:
        asyncio.run(client.get_track_async("0" * 22))

    assert excinfo.value.kind is ErrorKind.TRACK_NOT_FOUND

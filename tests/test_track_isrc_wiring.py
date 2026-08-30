"""The ISRC has to actually reach TrackMetadata.

get_track_async() used to hardcode isrc="" and the only code that filled it
in, enrich_track_async(), was never called from anywhere. Nothing failed —
it just quietly turned off everything downstream that keys on an ISRC:

  - playlist_sync.find_existing_track() falls back to title+artist
  - local_processor._with_musicbrainz_tags() returns early, so a re-tag
    writes none of MusicBrainz's ~25 tags
  - local_matcher's identity check cannot recognise its own candidates
  - files SpotiFLAC writes carry no ISRC for the next run to match on

A silent regression of exactly that shape is what these tests exist to
catch, so they assert the wiring rather than the network.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.spotify_metadata import SpotifyMetadataClient

ISRC = "USUM70504267"


class _FakeWebClient:
    """Only the two calls get_track_async makes off the GraphQL payload."""

    def __init__(self, isrc: str = ISRC, raises: bool = False) -> None:
        self.isrc = isrc
        self.raises = raises
        self.asked_for: list[str] = []

    def get_isrc_from_metadata(self, track_id: str) -> str:
        self.asked_for.append(track_id)
        if self.raises:
            raise RuntimeError("spclient unavailable")
        return self.isrc

    def get_track_composer(self, track_id: str) -> str:
        return ""

    def extract_cover_url(self, _art) -> str:
        return ""


def _client(web: _FakeWebClient) -> SpotifyMetadataClient:
    client = SpotifyMetadataClient.__new__(SpotifyMetadataClient)
    client.web_client = web  # type: ignore[attr-defined]
    return client


def _graphql_payload() -> dict:
    return {
        "data": {
            "trackUnion": {
                "name": "Window Shopper",
                "firstArtist": {"items": [{"profile": {"name": "50 Cent"}}]},
                "albumOfTrack": {"name": "Window Shopper"},
                "duration": {"totalMilliseconds": 192_240},
                "trackNumber": 1,
            }
        }
    }


def _patch_query(monkeypatch, client, payload) -> None:
    async def _fake_query(_payload):
        return payload

    monkeypatch.setattr(
        "SpotiFLAC.core.spotify_metadata.asyncio.to_thread",
        _make_to_thread(client),
    )
    client.web_client.query = lambda _p: payload  # type: ignore[attr-defined]


def _make_to_thread(client):
    async def _to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    return _to_thread


def test_the_isrc_reaches_the_track(monkeypatch) -> None:
    web = _FakeWebClient()
    client = _client(web)
    _patch_query(monkeypatch, client, _graphql_payload())

    track = asyncio.run(client.get_track_async("0" * 22))

    assert web.asked_for == [
        "0" * 22
    ], "get_track_async must look the ISRC up; it used to hardcode isrc=''"
    assert track.isrc == ISRC


def test_a_failed_isrc_lookup_does_not_fail_the_track(monkeypatch) -> None:
    """An ISRC is worth having, never worth losing the metadata over."""
    web = _FakeWebClient(raises=True)
    client = _client(web)
    _patch_query(monkeypatch, client, _graphql_payload())

    track = asyncio.run(client.get_track_async("0" * 22))

    assert track.isrc == ""
    assert track.title == "Window Shopper"


def test_musicbrainz_enrichment_is_gated_on_the_isrc() -> None:
    """Why the wiring above matters: this gate is why a re-tag wrote none of
    MusicBrainz's tags while every track carried isrc="".
    """
    from SpotiFLAC.core.local_processor import _with_musicbrainz_tags
    from SpotiFLAC.core.models import TrackMetadata
    from SpotiFLAC.core.tagger import EmbedOptions

    without = TrackMetadata(
        id="0" * 22, title="t", artists="a", album="", album_artist="a", isrc=""
    )
    opts = asyncio.run(_with_musicbrainz_tags(without, EmbedOptions()))
    assert not opts.extra_tags, "no ISRC means MusicBrainz is never consulted"


@pytest.mark.parametrize("isrc", ["", "not-an-isrc"])
def test_find_existing_track_cannot_use_a_missing_isrc(isrc) -> None:
    """The download-time "do I already have this?" check tries the ISRC
    first. With an empty one it silently degrades to text matching.
    """
    from SpotiFLAC.core.isrc_utils import normalize_isrc

    assert normalize_isrc(isrc) == ""

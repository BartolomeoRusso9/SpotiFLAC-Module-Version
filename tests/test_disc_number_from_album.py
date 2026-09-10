"""A playlist's track on disc 2 must not be tagged disc 1.

Playlist track data says disc 1 for every track. The real disc number was
read only from the native *track* metadata the ISRC lookup leaves in memory —
which stays empty when the ISRC came from the on-disk cache or the tracks
already had one, leaving every disc-2 track as disc 1. The album's native
metadata lists every disc's tracks, so it answers the question for all of
them at once: one request per album at most.
"""

from __future__ import annotations

import asyncio

import SpotiFLAC.downloader as downloader_mod
from SpotiFLAC.core import spotfetch
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.core.spotify_protobuf import id_to_gid_hex, parse_album
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader

# Low base62 IDs, so they round-trip through a 16-byte gid.
T1 = "0000000000000000000001"
T2 = "0000000000000000000002"
T3 = "0000000000000000000003"


# --- parse_album: which disc each track is on ------------------------------


def _varint(number: int) -> bytes:
    out = bytearray()
    while True:
        byte = number & 0x7F
        number >>= 7
        out.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(out)


def _len_field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _sint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value << 1)  # zigzag, value >= 0


def _disc(number: int, *track_ids: str) -> bytes:
    tracks = b"".join(
        _len_field(3, _len_field(1, bytes.fromhex(id_to_gid_hex(t)))) for t in track_ids
    )
    return _sint_field(1, number) + tracks


def test_the_album_says_which_disc_each_track_is_on() -> None:
    album = _len_field(11, _disc(1, T1, T2)) + _len_field(11, _disc(2, T3))

    parsed = parse_album(album)

    assert parsed["track_discs"] == {T1: 1, T2: 1, T3: 2}
    assert parsed["total_discs"] == 2
    assert parsed["total_tracks"] == 3


def test_an_album_without_a_disc_list_maps_nothing() -> None:
    assert parse_album(b"")["track_discs"] == {}


# --- the downloader, with nothing in the track cache ------------------------


class _NoIsrcLookup:
    def __init__(self, _http) -> None:
        raise AssertionError("every track already has its ISRC")


class _AlbumOnlyWebClient:
    def __init__(self) -> None:
        self.albums: list[str] = []

    def get_native_album_metadata(self, album_id: str) -> dict:
        self.albums.append(album_id)
        return {"release_date": "2001", "track_discs": {T1: 1, T2: 2}}


class _Client:
    def __init__(self, web_client) -> None:
        self.web_client = web_client


def _track(track_id: str) -> TrackMetadata:
    return TrackMetadata(
        id=track_id,
        title=track_id,
        artists="Artist",
        album="Album",
        album_artist="Artist",
        album_id="D2",
        isrc=f"KEEP{track_id[-1]}",
        release_date="2001",
        external_url=f"https://open.spotify.com/track/{track_id}",
    )


def test_disc_two_survives_a_cold_track_cache(tmp_path, monkeypatch) -> None:
    """The scenario from review: ISRCs already present, nothing cached."""
    downloader = SpotiflacDownloader(DownloadOptions(output_dir=str(tmp_path)))
    web = _AlbumOnlyWebClient()
    monkeypatch.setattr(downloader_mod, "IsrcHelper", _NoIsrcLookup)
    monkeypatch.setattr(spotfetch, "peek_native_track_metadata", lambda _id: None)
    monkeypatch.setattr(downloader, "_metadata_client", lambda: _Client(web))

    result = asyncio.run(downloader._resolve_isrc_bulk_async([_track(T1), _track(T2)]))

    assert [t.disc_number for t in result] == [1, 2]
    assert [t.isrc for t in result] == ["KEEP1", "KEEP2"], "retained, not looked up"
    assert web.albums == ["D2"], "one request for the album, not one per track"


def test_a_warm_track_cache_needs_no_album_request(tmp_path, monkeypatch) -> None:
    downloader = SpotiflacDownloader(DownloadOptions(output_dir=str(tmp_path)))
    web = _AlbumOnlyWebClient()
    monkeypatch.setattr(downloader_mod, "IsrcHelper", _NoIsrcLookup)
    monkeypatch.setattr(
        spotfetch, "peek_native_track_metadata", lambda _id: {"disc_number": 2}
    )
    monkeypatch.setattr(downloader, "_metadata_client", lambda: _Client(web))

    result = asyncio.run(downloader._resolve_isrc_bulk_async([_track(T1)]))

    assert result[0].disc_number == 2
    assert web.albums == []

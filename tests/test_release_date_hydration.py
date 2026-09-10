"""A playlist's release dates must not cost a request per track.

Tracks read from a playlist carry no release date, and _resolve_isrc_bulk_async
used to fill it in with a full get_track_async() per track — a GraphQL query,
a composer lookup and the ISRC over again — for a date every track of an album
shares. It now reads the date from the album's native metadata, once per
distinct album.
"""

from __future__ import annotations

import asyncio

import pytest

import SpotiFLAC.downloader as downloader_mod
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader

ALBUM_DATES = {"A1": "2020-01-02", "A2": "2019", "A3": ""}


def _track(n: int, album_id: str = "", release_date: str = "") -> TrackMetadata:
    return TrackMetadata(
        id=f"t{n}",
        title=f"Title {n}",
        artists="Artist",
        album="Album",
        album_artist="Artist",
        album_id=album_id,
        release_date=release_date,
        external_url=f"https://open.spotify.com/track/t{n}",
    )


class _FakeIsrcHelper:
    def __init__(self, _http) -> None:
        pass

    async def get_isrc_async(self, track_id: str) -> str:
        return f"ISRC{track_id}"


class _FakeWebClient:
    def __init__(self, fail: bool = False) -> None:
        self.albums: list[str] = []
        self.tracks: list[str] = []
        self.fail = fail

    def get_native_album_metadata(self, album_id: str) -> dict:
        self.albums.append(album_id)
        if self.fail:
            raise RuntimeError("spclient down")
        return {"release_date": ALBUM_DATES.get(album_id, "")}

    def get_native_track_metadata(self, track_id: str) -> dict:
        self.tracks.append(track_id)
        return {"release_date": "2001-05-05"}


class _FakeMetadataClient:
    def __init__(self, web_client: _FakeWebClient) -> None:
        self.web_client = web_client

    async def get_track_async(self, track_id: str):
        raise AssertionError("a full per-track lookup is what this removes")


@pytest.fixture()
def hydrate(tmp_path, monkeypatch):
    def _run(tracks, fail: bool = False):
        downloader = SpotiflacDownloader(DownloadOptions(output_dir=str(tmp_path)))
        web = _FakeWebClient(fail=fail)
        monkeypatch.setattr(downloader_mod, "IsrcHelper", _FakeIsrcHelper)
        monkeypatch.setattr(downloader_mod, "AsyncHttpClient", lambda *a, **k: None)
        monkeypatch.setattr(
            downloader, "_metadata_client", lambda: _FakeMetadataClient(web)
        )
        result = asyncio.run(downloader._resolve_isrc_bulk_async(list(tracks)))
        return result, web

    return _run


def test_one_album_request_dates_every_track_on_it(hydrate) -> None:
    tracks = [_track(1, "A1"), _track(2, "A1"), _track(3, "A2"), _track(4, "A1")]

    result, web = hydrate(tracks)

    assert sorted(web.albums) == ["A1", "A2"], "one request per album, not per track"
    assert web.tracks == []
    assert [t.release_date for t in result] == [
        "2020-01-02",
        "2020-01-02",
        "2019",
        "2020-01-02",
    ]
    assert [t.isrc for t in result] == ["ISRCt1", "ISRCt2", "ISRCt3", "ISRCt4"]


def test_a_track_with_no_album_id_uses_its_own_metadata(hydrate) -> None:
    result, web = hydrate([_track(1, album_id="")])

    assert web.albums == []
    assert web.tracks == ["t1"]
    assert result[0].release_date == "2001-05-05"


def test_a_date_already_there_is_not_looked_up_again(hydrate) -> None:
    """The album is still read once — for the disc number, which a playlist
    track lacks and nothing cached supplies — but the date is left alone."""
    result, web = hydrate([_track(1, "A1", release_date="1999")])

    assert web.albums == ["A1"] and web.tracks == []
    assert result[0].release_date == "1999"


def test_an_album_with_no_date_leaves_its_tracks_undated(hydrate) -> None:
    result, _web = hydrate([_track(1, "A3")])
    assert result[0].release_date == ""


def test_a_track_with_an_isrc_already_still_gets_its_date(hydrate) -> None:
    """Regression: with nothing missing an ISRC the whole step returned
    early, dates and disc numbers included."""
    track = _track(1, "A1").model_copy(update={"isrc": "KEEP"})

    result, web = hydrate([track])

    assert result[0].isrc == "KEEP"
    assert result[0].release_date == "2020-01-02"
    assert web.albums == ["A1"]


def test_a_failing_album_lookup_is_not_fatal(hydrate) -> None:
    result, web = hydrate([_track(1, "A1"), _track(2, "A2")], fail=True)

    assert sorted(web.albums) == ["A1", "A2"]
    assert [t.release_date for t in result] == ["", ""]
    assert [t.isrc for t in result] == ["ISRCt1", "ISRCt2"]

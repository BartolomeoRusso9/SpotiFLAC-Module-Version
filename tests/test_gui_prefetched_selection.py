"""Tracks picked in the GUI must not be looked up again one by one.

The GUI fetched the whole list to show it, but a partial selection reached the
downloader as bare links, and _resolve_track_list_async() resolved each one
with a full metadata lookup — several requests a track. The GUI now hands over
the metadata it already has. Of what such a lookup used to bring along, the
disc number is read from the native metadata the ISRC lookup caches, and the
composer — which that metadata does not carry — costs one credits request.
"""

from __future__ import annotations

import asyncio

import pytest

import SpotiFLAC as spotiflac_pkg
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.core import spotfetch
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader

PLAYLIST = "https://open.spotify.com/playlist/abc"


def _track(n: int, **extra) -> TrackMetadata:
    return TrackMetadata(
        id=f"t{n}",
        title=f"Title {n}",
        artists="Artist",
        album="Album",
        album_artist="Artist",
        external_url=f"https://open.spotify.com/track/t{n}",
        **extra,
    )


# --- the GUI hands its metadata over ----------------------------------------


@pytest.fixture()
def download(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", lambda **kw: seen.append(kw))

    def _run(indices, url=PLAYLIST, tracks=None):
        api = SpotiFLAC_API()
        api.download_dir = str(tmp_path)
        api.current_tracks = (
            tracks if tracks is not None else [_track(n) for n in range(4)]
        )
        api.current_url = url
        api._download_task(indices, {"services": ["tidal"]})
        return seen[-1]

    return _run


def test_a_selection_carries_the_metadata_already_fetched(download) -> None:
    call = download([0, 2, 3])

    assert call["batch_tracks"] is True
    prefetched = call["prefetched_tracks"]
    assert list(prefetched) == call["url"]
    assert [t.id for t in prefetched.values()] == ["t0", "t2", "t3"]


def test_a_csv_list_is_still_looked_up(download) -> None:
    """A CSV row carries little more than a title; the lookup is the point."""
    call = download([0, 1], url="")
    assert call["prefetched_tracks"] is None


def test_the_whole_collection_still_goes_as_its_url(download) -> None:
    call = download([0, 1, 2, 3])
    assert call["url"] == PLAYLIST
    assert call["prefetched_tracks"] is None


def test_the_sync_wrapper_forwards_what_it_is_given(monkeypatch) -> None:
    from SpotiFLAC import client as client_mod

    seen: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> None:
            return None

        async def download_tracks(self, urls, *, loop_minutes=None, prefetched=None):
            seen["prefetched"] = prefetched

    monkeypatch.setattr(client_mod, "AsyncSpotiFLAC", _FakeClient)
    known = {"u1": _track(1)}
    client_mod.SpotiFLAC(
        url=["u1", "u2"],
        output_dir="/tmp/out",
        batch_tracks=True,
        prefetched_tracks=known,
    )
    assert seen["prefetched"] == known


# --- the downloader does not look them up again ----------------------------


def test_prefetched_links_are_not_resolved_again(tmp_path, monkeypatch) -> None:
    downloader = SpotiflacDownloader(DownloadOptions(output_dir=str(tmp_path)))
    looked_up: list[str] = []
    history: list[tuple] = []
    runs: list[list[str]] = []

    async def _lookup(url):
        looked_up.append(url)
        return "Looked up", [_track(9)], {"type": "track"}

    async def _history(url, name, tracks, info):
        history.append((url, name, info.get("type")))

    async def _worker(tracks, *args, **kwargs):
        runs.append([t.id for t in tracks])
        return []

    async def _identity(tracks):
        return tracks

    monkeypatch.setattr(downloader, "_resolve_metadata_async", _lookup)
    monkeypatch.setattr(downloader, "_record_history_async", _history)
    monkeypatch.setattr(downloader, "_run_worker_async", _worker)
    monkeypatch.setattr(downloader, "_resolve_isrc_bulk_async", _identity)
    credits = _FakeCredits()
    monkeypatch.setattr(downloader, "_metadata_client", lambda: credits)

    urls = ["https://open.spotify.com/track/t1", "https://open.spotify.com/track/t9"]
    asyncio.run(downloader.run_tracks_async(urls, prefetched={urls[0]: _track(1)}))

    assert looked_up == [urls[1]], "only the link without metadata is looked up"
    assert runs == [["t1", "t9"]]
    # A selection leaves the recent links alone: the list it came from is
    # already there, and a line per downloaded song buried it.
    assert history == []
    # The composer is the one thing it still asks for — once, for that track.
    assert credits.web_client.asked == ["t1"]


class _FakeWebClient:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_track_composer(self, track_id: str) -> str:
        self.asked.append(track_id)
        return "Writer"


class _FakeCredits:
    def __init__(self) -> None:
        self.web_client = _FakeWebClient()


def _composer_of(track: TrackMetadata, tmp_path) -> tuple[str, list[str]]:
    downloader = SpotiflacDownloader(DownloadOptions(output_dir=str(tmp_path)))
    credits = _FakeCredits()
    downloader._metadata_client = lambda: credits
    result = asyncio.run(downloader._with_composer_async(track, asyncio.Semaphore(1)))
    return result.composer, credits.web_client.asked


def test_a_hand_picked_track_gets_its_composer(tmp_path) -> None:
    assert _composer_of(_track(1), tmp_path) == ("Writer", ["t1"])


def test_a_composer_already_known_is_not_asked_for(tmp_path) -> None:
    assert _composer_of(_track(1, composer="Mine"), tmp_path) == ("Mine", [])


def test_only_spotify_tracks_are_asked_about(tmp_path) -> None:
    other = _track(1).model_copy(
        update={"external_url": "https://tidal.com/browse/track/1"}
    )
    assert _composer_of(other, tmp_path) == ("", [])


# --- composer and disc number from the cache the ISRC lookup filled ---------


def test_composer_and_disc_come_from_the_cache_without_a_request(monkeypatch) -> None:
    cached = {"t1": {"composer": "Writer", "disc_number": 2}}
    monkeypatch.setattr(spotfetch, "peek_native_track_metadata", cached.get)

    filled = SpotiflacDownloader._fill_from_native_cache([_track(1), _track(2)])

    assert (filled[0].composer, filled[0].disc_number) == ("Writer", 2)
    # Nothing cached for t2: left as it was, and no lookup made for it.
    assert (filled[1].composer, filled[1].disc_number) == ("", 1)


def test_what_a_track_already_has_is_not_overwritten(monkeypatch) -> None:
    monkeypatch.setattr(
        spotfetch,
        "peek_native_track_metadata",
        lambda _id: {"composer": "Other", "disc_number": 3},
    )
    track = _track(1, composer="Mine", disc_number=2)

    assert SpotiflacDownloader._fill_from_native_cache([track])[0] is track

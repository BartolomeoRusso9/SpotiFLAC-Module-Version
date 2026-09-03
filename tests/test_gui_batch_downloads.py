"""The GUI must download a selection as one run, not one run per track.

A hand-picked selection cannot be expressed as a collection URL, so
_download_task built a list of track links — and then called
client.SpotiFLAC() once per link. Each call is a one-shot session (its own
event loop, httpx pool, ExtensionManager and provider set), so the console
showed a full "[RUN] 1 track(s)" pipeline per track, the provider bootstrap
ran again for every one of them, and max_concurrent_downloads had nothing to
work with: a pool holding one track has nothing to download beside it.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import SpotiFLAC as spotiflac_pkg
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader


class _FakeTrack:
    def __init__(self, n: int) -> None:
        self.id = f"track-{n}"
        self.title = f"Title {n}"
        self.external_url = f"https://open.spotify.com/track/track-{n}"


@pytest.fixture()
def captured_calls(tmp_path, monkeypatch):
    """Runs _download_task and returns every call the wrapper received."""
    seen: list[dict] = []

    def _fake_spotiflac(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", _fake_spotiflac)

    def _run(indices, config=None, tracks=3, url=""):
        before = len(seen)
        api = SpotiFLAC_API()
        api.download_dir = str(tmp_path)
        api.current_tracks = [_FakeTrack(n) for n in range(tracks)]
        api.current_url = url
        api._download_task(indices, {"services": ["tidal"], **(config or {})})
        calls = seen[before:]
        assert calls, "_download_task never reached the download call"
        return calls

    return _run


def test_a_selection_is_one_call_carrying_every_track(captured_calls) -> None:
    calls = captured_calls([0, 1, 2])
    assert len(calls) == 1, "one session per track is what this change removes"
    assert calls[0]["batch_tracks"] is True
    assert calls[0]["url"] == [_FakeTrack(n).external_url for n in range(3)]


def test_a_whole_collection_still_goes_as_its_own_url(captured_calls) -> None:
    """The collection shortcut resolves to a single link, which the
    downloader expands itself — that path must stay untouched, since it is
    what creates playlist subfolders and the M3U file."""
    url = "https://open.spotify.com/playlist/abc"
    calls = captured_calls([0, 1, 2], url=url)
    assert len(calls) == 1
    assert calls[0]["url"] == url
    assert calls[0]["batch_tracks"] is False


def test_a_single_track_is_not_wrapped_in_a_list(captured_calls) -> None:
    calls = captured_calls([1])
    assert calls[0]["url"] == _FakeTrack(1).external_url
    assert calls[0]["batch_tracks"] is False


def test_parallel_downloads_setting_reaches_the_client(captured_calls) -> None:
    assert captured_calls([0])[0]["max_concurrent_downloads"] == 2
    opts = captured_calls([0], {"max_concurrent_downloads": 5})[0]
    assert opts["max_concurrent_downloads"] == 5


@pytest.mark.parametrize(
    ("sent", "expected"),
    # 0 and None both read as "not set" — an empty number input sends one or
    # the other — and fall back to the default rather than to 1.
    [(0, 2), (None, 2), (-3, 1), (999, 8), ("4", 4)],
)
def test_the_setting_is_clamped(captured_calls, sent, expected) -> None:
    """In --web mode this config dict is an HTTP body, so the value is
    whatever the caller felt like sending."""
    opts = captured_calls([0], {"max_concurrent_downloads": sent})[0]
    assert opts["max_concurrent_downloads"] == expected


@pytest.mark.parametrize(
    "picked", ["HI_RES_LOSSLESS", "LOSSLESS", "DOLBY_ATMOS"]
)
def test_the_quality_picker_reaches_the_client_untouched(captured_calls, picked) -> None:
    assert captured_calls([0], {"quality": picked})[0]["quality"] == picked


def test_quality_survives_the_trip_to_each_provider() -> None:
    """The GUI sends a canonical name; each provider is handed its own token
    for it. This is the hop where a picked HI_RES_LOSSLESS could silently
    become a CD-quality request without anything logging a word.
    """
    from SpotiFLAC.core.quality import normalize_quality, quality_for_provider

    q = "HI_RES_LOSSLESS"
    assert normalize_quality(q) == q
    # ext:*-web / ext:*-py are the same providers wearing an extension name.
    assert quality_for_provider("ext:tidal-web", q) == "HI_RES_LOSSLESS"
    assert quality_for_provider("tidal", q) == "HI_RES_LOSSLESS"
    assert quality_for_provider("qobuz", q) == "27"
    assert quality_for_provider("amazon", q) == "best"
    assert quality_for_provider("deezer", q) == "FLAC"
    # …and LOSSLESS must not quietly reach Qobuz as the hi-res tier.
    assert quality_for_provider("qobuz", "LOSSLESS") == "6"


def test_two_batches_never_run_at_the_same_time(tmp_path, monkeypatch) -> None:
    """The frontend starts a batch per click and does not serialize them.
    Two sessions at once means two independent semaphores and, worse, a
    fight over the process-wide console interception in core/progress.py.
    """
    running = 0
    overlapped = False

    def _fake_spotiflac(**kwargs):
        nonlocal running, overlapped
        running += 1
        overlapped = overlapped or running > 1
        time.sleep(0.15)
        running -= 1

    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", _fake_spotiflac)

    api = SpotiFLAC_API()
    api.download_dir = str(tmp_path)
    api.current_tracks = [_FakeTrack(n) for n in range(2)]
    api.current_url = ""

    threads = [
        threading.Thread(target=api._download_task, args=([i], {"services": ["t"]}))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "a batch never released the lock"
    assert not overlapped


def test_the_finished_event_names_the_batch_it_closes(tmp_path, monkeypatch) -> None:
    """app_download_finished() used to close every 'active' queue row, which
    with a batch waiting its turn meant reporting tracks done before they
    had started."""
    pushed: list[tuple] = []
    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", lambda **kwargs: None)

    api = SpotiFLAC_API()
    api.download_dir = str(tmp_path)
    api.current_tracks = [_FakeTrack(n) for n in range(3)]
    api.current_url = ""
    monkeypatch.setattr(api, "_push", lambda name, *args: pushed.append((name, args)))

    api._download_task([0, 2], {"services": ["tidal"]})

    finished = [args for name, args in pushed if name == "app_download_finished"]
    assert finished == [(True, [0, 2])]


def test_the_sync_wrapper_routes_a_batch_to_the_single_run_path(monkeypatch) -> None:
    """client.SpotiFLAC() is what the GUI calls; batch_tracks is what tells
    it the URLs are tracks and not one collection each."""
    from SpotiFLAC import client as client_mod

    seen: dict[str, list[str]] = {}

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            seen["kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> None:
            return None

        async def download_tracks(self, urls, *, loop_minutes=None) -> None:
            seen["tracks"] = urls

        async def download_batch(self, urls, *, loop_minutes=None) -> None:
            seen["batch"] = urls

    monkeypatch.setattr(client_mod, "AsyncSpotiFLAC", _FakeClient)

    client_mod.SpotiFLAC(
        url=["u1", "u2"], output_dir="/tmp/out", batch_tracks=True
    )
    assert seen["tracks"] == ["u1", "u2"]
    assert "batch" not in seen

    seen.clear()
    # Default off: a caller that predates the flag keeps one run per URL.
    client_mod.SpotiFLAC(url=["p1", "p2"], output_dir="/tmp/out")
    assert seen["batch"] == ["p1", "p2"]
    assert "tracks" not in seen


def _meta(track_id: str) -> TrackMetadata:
    return TrackMetadata(
        id=track_id, title=track_id, artists="A", album="Alb", album_artist="A"
    )


@pytest.fixture()
def batching_downloader(tmp_path, monkeypatch):
    """A downloader whose metadata/worker layers are recorded, not run."""
    downloader = SpotiflacDownloader(DownloadOptions(output_dir=str(tmp_path)))
    runs: list[dict] = []

    async def _fake_metadata(url):
        if "missing" in url:
            from SpotiFLAC.core.errors import ErrorKind, SpotiflacError

            raise SpotiflacError(ErrorKind.TRACK_NOT_FOUND, "gone")
        track_id = url.rsplit("/", 1)[-1]
        return f"Album {track_id}", [_meta(track_id)], {"type": "track"}

    async def _fake_worker(tracks, collection_name, info, is_album, is_playlist, **kw):
        runs.append(
            {
                "tracks": [t.id for t in tracks],
                "is_album": is_album,
                "is_playlist": is_playlist,
            }
        )
        return []

    monkeypatch.setattr(downloader, "_resolve_metadata_async", _fake_metadata)
    monkeypatch.setattr(downloader, "_run_worker_async", _fake_worker)
    monkeypatch.setattr(
        downloader, "_resolve_isrc_bulk_async", lambda tracks: _identity(tracks)
    )
    monkeypatch.setattr(downloader, "_record_history_async", _noop)
    return downloader, runs


async def _identity(tracks):
    return tracks


async def _noop(*args, **kwargs):
    return None


def test_run_tracks_async_uses_one_worker_for_every_link(batching_downloader) -> None:
    downloader, runs = batching_downloader
    urls = [f"https://open.spotify.com/track/t{n}" for n in range(4)]

    asyncio.run(downloader.run_tracks_async(urls))

    assert len(runs) == 1, "one worker run, not one per URL"
    assert runs[0]["tracks"] == ["t0", "t1", "t2", "t3"]
    # The tracks keep the layout they had when each got a run of its own:
    # batching must not start creating playlist folders.
    assert runs[0]["is_album"] is False
    assert runs[0]["is_playlist"] is False


def test_an_unresolvable_link_does_not_sink_the_batch(batching_downloader) -> None:
    downloader, runs = batching_downloader
    asyncio.run(
        downloader.run_tracks_async(
            [
                "https://open.spotify.com/track/t0",
                "https://open.spotify.com/track/missing",
                "https://open.spotify.com/track/t1",
            ]
        )
    )
    assert runs[0]["tracks"] == ["t0", "t1"]


def test_the_same_track_twice_is_downloaded_once(batching_downloader) -> None:
    downloader, runs = batching_downloader
    url = "https://open.spotify.com/track/t0"
    asyncio.run(downloader.run_tracks_async([url, url]))
    assert runs[0]["tracks"] == ["t0"]


def test_nothing_resolvable_means_no_run_at_all(batching_downloader) -> None:
    downloader, runs = batching_downloader
    asyncio.run(
        downloader.run_tracks_async(["https://open.spotify.com/track/missing"])
    )
    assert runs == []

"""api_mixins/subscriptions.py — what a check turns into for the GUI.

"Check & download" used to hand each new release to fetch_metadata(), which
only loads a tracklist into the page: nothing was downloaded, and each
release replaced the last one on screen. New items are now queued as real
download jobs — a playlist's added tracks as one job, an artist's releases one
whole job each — and scheduled checks do the same with saved settings.
"""

from __future__ import annotations

import pytest

from SpotiFLAC.api_mixins.subscriptions import SubscriptionsMixin
from SpotiFLAC.core import subscriptions as subs
from SpotiFLAC.core.models import TrackMetadata

PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
ARTIST_URL = "https://open.spotify.com/artist/0000000000000000000000"


def _track(track_id: str) -> TrackMetadata:
    return TrackMetadata(
        id=track_id,
        title=f"Song {track_id}",
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )


class FakeApi(SubscriptionsMixin):
    """The bits of SpotiFLAC_API the mixin leans on, recording instead."""

    def __init__(self, owner: str = "") -> None:
        self.owner = owner
        self.download_dir = "/music"
        self.jobs: list[dict] = []
        self.pushed: list[tuple] = []
        self.logs: list[tuple] = []
        self.release_tracks: dict[str, list] = {}

    def _start_download_job(self, payload: dict) -> None:
        self.jobs.append(payload)

    def _push(self, fn_name: str, *args) -> None:
        self.pushed.append((fn_name, args))

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append((level, message))

    def _release_tracks(self, url: str) -> list:
        return self.release_tracks[url]


@pytest.fixture
def playlist(monkeypatch):
    """The playlist's current tracks; edit the list to add some."""
    tracks = [_track("t1")]

    async def fake_listing(url, *, client=None):
        return "My Playlist", list(tracks)

    monkeypatch.setattr(subs, "list_playlist_tracks_async", fake_listing)
    return tracks


def test_a_scheduled_check_queues_only_the_added_tracks(playlist):
    api = FakeApi()
    sub = subs.add(
        PLAYLIST_URL, interval_minutes=15, download_config={"quality": "LOSSLESS"}
    )

    api._run_scheduled_check(sub)  # first check: the baseline
    assert api.jobs == []

    playlist.append(_track("t2"))
    api._run_scheduled_check(subs.get(sub.id))

    [job] = api.jobs
    assert [t["id"] for t in job["tracks"]] == ["t2"]
    assert job["indices"] == [0]
    assert job["source_url"] == PLAYLIST_URL
    assert job["whole"] is False
    assert job["config"] == {"quality": "LOSSLESS"}
    fn_name, (payload,) = api.pushed[-1]
    assert fn_name == "subscriptionsChecked"
    assert payload["scheduled"] is True and payload["new"] == 1


def test_check_and_download_queues_each_new_release_whole(monkeypatch):
    api = FakeApi()
    sub = subs.add(ARTIST_URL)
    subs.mark_seen(sub.id, [subs.Release(id="old")])  # not a first check
    releases = [subs.Release(id="old"), subs.Release(id="new1", title="New")]

    async def fake_releases(url, groups, *, client=None):
        return "Artist", releases

    monkeypatch.setattr(subs, "list_releases_async", fake_releases)
    api.release_tracks[releases[1].url] = [_track("a"), _track("b")]

    api._check_subscriptions_thread(True, {"quality": "HI_RES_LOSSLESS"})

    [job] = api.jobs
    assert job["source_url"] == "https://open.spotify.com/album/new1"
    assert job["whole"] is True
    assert [t["id"] for t in job["tracks"]] == ["a", "b"]
    assert job["config"] == {"quality": "HI_RES_LOSSLESS"}
    # The settings sent with a manual download are kept for scheduled ones.
    assert subs.get(sub.id).download_config == {"quality": "HI_RES_LOSSLESS"}


def test_check_for_new_does_not_download(playlist):
    api = FakeApi()
    subs.add(PLAYLIST_URL)
    api._check_subscriptions_thread(False)
    playlist.append(_track("t2"))

    api._check_subscriptions_thread(False)

    assert api.jobs == []
    assert api.pushed[-1][1][0]["new"] == 1


def test_subscriptions_are_scoped_to_their_owner():
    alice, bob = FakeApi("alice"), FakeApi("bob")
    created = alice.add_subscription(PLAYLIST_URL, interval_minutes=60)["subscription"]

    assert [s["id"] for s in alice.get_subscriptions()] == [created["id"]]
    assert bob.get_subscriptions() == []
    assert bob.set_subscription_interval(created["id"], 15)["ok"] is False
    assert subs.get(created["id"]).interval_minutes == 60


def test_set_subscription_interval_saves_the_settings():
    api = FakeApi()
    sub = api.add_subscription(PLAYLIST_URL)["subscription"]

    res = api.set_subscription_interval(sub["id"], 30, {"quality": "LOSSLESS"})

    assert res["ok"] is True
    assert res["subscription"]["interval_minutes"] == 30
    assert subs.get(sub["id"]).download_config == {"quality": "LOSSLESS"}
    assert api.set_subscription_interval(sub["id"], 5)["ok"] is False


def test_the_scheduler_dispatches_to_the_owners_instance(playlist):
    host = FakeApi()
    bob = FakeApi("bob")
    sub = subs.add(PLAYLIST_URL, owner="bob", interval_minutes=15)
    subs.mark_seen(sub.id, [subs.Release(id="t1", type="track")])
    playlist.append(_track("t2"))
    instances = {"bob": bob}

    scheduler = host._start_subscription_scheduler(instances.__getitem__)
    try:
        assert host._start_subscription_scheduler() is scheduler  # once
        assert scheduler.tick() == 1
    finally:
        scheduler.stop()

    assert host.jobs == []
    assert [t["id"] for t in bob.jobs[0]["tracks"]] == ["t2"]

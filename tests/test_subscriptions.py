"""Tests for core/subscriptions.py — following an artist, offline."""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import subscriptions as subs
from SpotiFLAC.core.subscriptions import Release, SubscriptionError

ARTIST_URL = "https://open.spotify.com/artist/0000000000000000000000"


class FakeWebClient:
    """Stands in for SpotifyWebClient.get_artist_discography."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.calls = 0

    def get_artist_discography(self, artist_id: str) -> list[dict]:
        self.calls += 1
        return self._items


class FakeMetadataClient:
    def __init__(self, items: list[dict], name: str = "Test Artist") -> None:
        self.web_client = FakeWebClient(items)
        self._name = name

    async def get_artist_profile_async(self, artist_id: str) -> dict:
        return {"profile": {"name": self._name}}


def _release_item(release_id: str, name: str, rtype: str = "ALBUM", year: int = 2026):
    """Shaped like one item of Spotify's discography GraphQL response."""
    return {
        "releases": {
            "items": [
                {
                    "id": release_id,
                    "name": name,
                    "type": rtype,
                    "date": {"year": year},
                }
            ]
        }
    }


# ── Store ─────────────────────────────────────────────────────────────────


def test_add_is_idempotent_on_url():
    first = subs.add(ARTIST_URL, name="A", output_dir="/music")
    second = subs.add(ARTIST_URL, name="A renamed")

    assert first.id == second.id
    assert len(subs.list_all()) == 1
    # An update must not blank fields the second call didn't mention.
    assert second.output_dir == "/music"
    assert second.name == "A renamed"


def test_add_rejects_unknown_release_groups():
    with pytest.raises(SubscriptionError):
        subs.add(ARTIST_URL, include_groups="album,bootlegs")


def test_include_groups_all_expands():
    sub = subs.add(ARTIST_URL, include_groups="all")
    assert set(sub.groups) == set(subs.RELEASE_GROUPS)


def test_remove_takes_the_seen_set_with_it():
    sub = subs.add(ARTIST_URL)
    subs.mark_seen(sub.id, [Release(id="r1"), Release(id="r2")])
    assert subs.count_seen(sub.id) == 2

    assert subs.remove(sub.id) is True
    assert subs.count_seen(sub.id) == 0
    assert subs.get(sub.id) is None


def test_seen_set_is_a_set():
    sub = subs.add(ARTIST_URL)
    subs.mark_seen(sub.id, [Release(id="r1")])
    subs.mark_seen(sub.id, [Release(id="r1"), Release(id="r2")])
    assert subs.seen_ids(sub.id) == {"r1", "r2"}


def test_enabled_only_filters_the_listing():
    sub = subs.add(ARTIST_URL)
    subs.set_enabled(sub.id, False)
    assert subs.list_all() != []
    assert subs.list_all(enabled_only=True) == []


# ── Checking ──────────────────────────────────────────────────────────────


def test_first_check_watermarks_instead_of_backfilling():
    sub = subs.add(ARTIST_URL)
    client = FakeMetadataClient(
        [_release_item("a1", "First"), _release_item("a2", "Second")]
    )

    result = asyncio.run(subs.check_async(sub, client=client))

    assert result.watermarked is True
    assert result.new == []
    assert result.total == 2
    assert subs.seen_ids(sub.id) == {"a1", "a2"}
    assert result.artist_name == "Test Artist"


def test_first_check_with_backfill_returns_everything():
    sub = subs.add(ARTIST_URL)
    client = FakeMetadataClient([_release_item("a1", "First")])

    result = asyncio.run(subs.check_async(sub, backfill=True, client=client))

    assert result.watermarked is False
    assert [r.id for r in result.new] == ["a1"]


def test_second_check_returns_only_what_appeared_since():
    sub = subs.add(ARTIST_URL)
    client = FakeMetadataClient([_release_item("a1", "First")])
    asyncio.run(subs.check_async(sub, client=client))

    client.web_client._items.append(_release_item("a2", "Brand New", "SINGLE", 2027))
    result = asyncio.run(subs.check_async(sub, client=client))

    assert [r.id for r in result.new] == ["a2"]
    assert result.new[0].title == "Brand New"
    assert result.new[0].type == "single"
    assert result.new[0].year == "2027"
    assert result.new[0].url.endswith("/album/a2")

    # And once seen, it is not offered again.
    assert asyncio.run(subs.check_async(sub, client=client)).new == []


def test_include_groups_filters_release_types():
    sub = subs.add(ARTIST_URL, include_groups="album")
    client = FakeMetadataClient(
        [
            _release_item("a1", "An album", "ALBUM"),
            _release_item("s1", "A single", "SINGLE"),
        ]
    )

    asyncio.run(subs.check_async(sub, backfill=True, client=client))
    assert subs.seen_ids(sub.id) == {"a1"}


def test_reset_makes_the_back_catalogue_new_again():
    sub = subs.add(ARTIST_URL)
    client = FakeMetadataClient([_release_item("a1", "First")])
    asyncio.run(subs.check_async(sub, client=client))

    subs.forget_seen(sub.id)
    # An emptied seen-set is a first check again, so backfill is what asks
    # for the catalogue rather than re-watermarking it.
    result = asyncio.run(subs.check_async(sub, backfill=True, client=client))
    assert [r.id for r in result.new] == ["a1"]


def test_check_records_the_error_and_does_not_raise():
    # An artist subscription pointing at an album: there is no discography.
    sub = subs.add("https://open.spotify.com/album/123", kind="artist")
    result = asyncio.run(subs.check_async(sub, client=FakeMetadataClient([])))

    assert result.error
    assert "album" in result.error
    assert subs.get(sub.id).last_error


def test_a_failing_release_does_not_stop_the_rest():
    sub = subs.add(ARTIST_URL, output_dir="/music")
    result = subs.CheckResult(
        subscription=sub,
        new=[Release(id="ok1"), Release(id="bad"), Release(id="ok2")],
    )
    fetched: list[str] = []

    async def download(url: str, output_dir: str) -> None:
        if "bad" in url:
            raise RuntimeError("provider exploded")
        fetched.append(url)

    dispatched = asyncio.run(subs.sync_async([result], download))

    assert dispatched == 2
    assert len(fetched) == 2


def test_discography_listing_is_not_fetched_per_album():
    """The check must stay cheap — one discography call, no album fan-out."""
    sub = subs.add(ARTIST_URL)
    client = FakeMetadataClient([_release_item(f"a{i}", f"R{i}") for i in range(50)])

    asyncio.run(subs.check_async(sub, client=client))

    assert client.web_client.calls == 1


# ── Playlists ─────────────────────────────────────────────────────────────

PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"


def _track(track_id: str, title: str = "Song"):
    from SpotiFLAC.core.models import TrackMetadata

    return TrackMetadata(
        id=track_id,
        title=title,
        artists="Artist",
        album="Album",
        album_artist="Artist",
    )


class FakePlaylistClient:
    """Stands in for SpotifyMetadataClient.get_url on a playlist link."""

    def __init__(self, tracks: list, name: str = "My Playlist") -> None:
        self.tracks = tracks
        self.name = name

    def get_url(self, url: str):
        return self.name, list(self.tracks), "", {}


def test_the_kind_is_read_off_the_link():
    assert subs.add(PLAYLIST_URL).kind == "playlist"
    assert subs.add(ARTIST_URL).kind == "artist"


def test_a_link_that_is_neither_is_refused():
    with pytest.raises(SubscriptionError, match="album"):
        subs.add("https://open.spotify.com/album/1")


def test_playlist_first_check_watermarks():
    sub = subs.add(PLAYLIST_URL)
    client = FakePlaylistClient([_track("t1"), _track("t2")])

    result = asyncio.run(subs.check_async(sub, client=client))

    assert result.watermarked
    assert result.new == [] and result.new_tracks == []
    assert result.total == 2
    assert subs.get(sub.id).name == "My Playlist"


def test_playlist_check_returns_only_the_added_tracks_with_metadata():
    sub = subs.add(PLAYLIST_URL)
    client = FakePlaylistClient([_track("t1")])
    asyncio.run(subs.check_async(sub, client=client))

    # Added once, listed twice: still one new track.
    client.tracks = [_track("t1"), _track("t2", "New one"), _track("t2", "New one")]
    result = asyncio.run(subs.check_async(sub, client=client))

    assert [r.id for r in result.new] == ["t2"]
    assert [t.id for t in result.new_tracks] == ["t2"]
    assert result.new[0].url == "https://open.spotify.com/track/t2"
    assert result.new[0].title == "New one — Artist"
    assert result.to_dict()["kind"] == "playlist"


# ── Schedules ─────────────────────────────────────────────────────────────


def test_an_interval_under_the_minimum_is_refused():
    with pytest.raises(SubscriptionError, match=str(subs.MIN_INTERVAL_MINUTES)):
        subs.add(PLAYLIST_URL, interval_minutes=5)


def test_re_adding_keeps_the_schedule_and_settings_unless_given():
    subs.add(PLAYLIST_URL, interval_minutes=60, download_config={"quality": "LOSSLESS"})

    again = subs.add(PLAYLIST_URL)

    assert again.interval_minutes == 60
    assert again.download_config == {"quality": "LOSSLESS"}


def test_due_lists_scheduled_subscriptions_whose_interval_ran_out():
    import time

    scheduled = subs.add(PLAYLIST_URL, interval_minutes=30)
    subs.add(ARTIST_URL)  # manual: never due

    assert [s.id for s in subs.due()] == [scheduled.id]  # never checked

    subs.record_check(scheduled.id)
    now = time.time()
    assert subs.due(now) == []
    assert [s.id for s in subs.due(now + 30 * 60 + 1)] == [scheduled.id]

    subs.set_enabled(scheduled.id, False)
    assert subs.due(now + 3600) == []


def test_set_schedule_saves_the_settings_and_zero_keeps_them():
    sub = subs.add(PLAYLIST_URL)

    assert subs.set_schedule(sub.id, 60, {"quality": "HI_RES_LOSSLESS"})
    stored = subs.get(sub.id)
    assert stored.interval_minutes == 60
    assert stored.download_config == {"quality": "HI_RES_LOSSLESS"}
    assert stored.to_dict()["next_check_at"] is not None

    subs.set_schedule(sub.id, 0)
    stored = subs.get(sub.id)
    assert stored.interval_minutes == 0
    assert stored.next_check_at is None
    assert stored.download_config == {"quality": "HI_RES_LOSSLESS"}


def test_saved_settings_are_updated_per_owner():
    mine = subs.add(PLAYLIST_URL, owner="me")
    theirs = subs.add(PLAYLIST_URL, owner="them")

    subs.update_download_config("me", {"quality": "HI_RES"})

    assert subs.get(mine.id).download_config == {"quality": "HI_RES"}
    assert subs.get(theirs.id).download_config == {}

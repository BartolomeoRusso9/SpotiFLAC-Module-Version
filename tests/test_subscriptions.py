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
    sub = subs.add("https://open.spotify.com/playlist/123")
    result = asyncio.run(subs.check_async(sub, client=FakeMetadataClient([])))

    assert result.error
    assert "playlist" in result.error
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

"""`core/tracklist.py` — a link, the tracks behind it, and which to fetch.

The interesting decision is `download_target()`. Selecting everything must
hand back the *collection* URL, not a list of track links: the collection
resolves once and keeps the album's own ordering and numbering, where a list
of individual tracks is exactly that. Getting it backwards would still
download the right audio and quietly file it wrong.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.tracklist import (
    Tracklist,
    download_target,
    is_link,
    metadata_client_for,
    resolve_tracklist,
    track_url,
    unresolved_titles,
)


class _Track:
    def __init__(self, title, external_url=None, track_id=None, artists="Miles Davis"):
        self.title = title
        self.artists = artists
        self.album = "Kind of Blue"
        if external_url is not None:
            self.external_url = external_url
        if track_id is not None:
            self.id = track_id


def _collection(*tracks, source="https://open.spotify.com/album/a1") -> Tracklist:
    return Tracklist(name="Kind of Blue", tracks=list(tracks), source_url=source)


# ---------------------------------------------------------------------------
# Client dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://tidal.com/browse/album/1", "TidalMetadataClient"),
        ("https://music.apple.com/us/album/x/1", "AppleMusicMetadataClient"),
        ("https://open.spotify.com/album/1", "SpotifyMetadataClient"),
        ("spotify:album:1", "SpotifyMetadataClient"),
        ("something unrecognised", "SpotifyMetadataClient"),
    ],
)
def test_the_client_matches_the_domain(url, expected) -> None:
    assert type(metadata_client_for(url)).__name__ == expected


def test_a_link_is_told_from_a_search() -> None:
    assert is_link("https://open.spotify.com/album/1")
    assert is_link("spotify:album:1")
    assert not is_link("miles davis kind of blue")
    assert not is_link("")


# ---------------------------------------------------------------------------
# Per-track links
# ---------------------------------------------------------------------------


def test_a_tracks_own_link_is_used_when_it_has_one() -> None:
    track = _Track("So What", external_url="https://open.spotify.com/track/t2")
    assert track_url(track) == "https://open.spotify.com/track/t2"


def test_an_id_is_rebuilt_against_the_service_it_came_from() -> None:
    """A track id means nothing without the service that issued it."""
    track = _Track("So What", track_id="t2")

    assert track_url(track, "https://open.spotify.com/album/a") == (
        "https://open.spotify.com/track/t2"
    )
    assert track_url(track, "https://tidal.com/browse/album/a") == (
        "https://tidal.com/browse/track/t2"
    )
    assert track_url(track, "https://music.apple.com/us/album/x/1") == (
        "https://music.apple.com/track/t2"
    )


def test_a_track_with_neither_link_nor_id_has_no_url() -> None:
    assert track_url(_Track("Bare Title")) == ""


# ---------------------------------------------------------------------------
# What the run fetches
# ---------------------------------------------------------------------------


def test_selecting_everything_keeps_the_collection_url() -> None:
    """One resolution, and the album's own ordering and numbering."""
    tracks = _collection(
        _Track("a", track_id="1"),
        _Track("b", track_id="2"),
        _Track("c", track_id="3"),
    )

    assert download_target(tracks, [0, 1, 2]) == "https://open.spotify.com/album/a1"
    assert download_target(tracks, None) == "https://open.spotify.com/album/a1"


def test_a_subset_becomes_a_list_of_track_links() -> None:
    tracks = _collection(
        _Track("a", track_id="1"),
        _Track("b", track_id="2"),
        _Track("c", track_id="3"),
    )

    assert download_target(tracks, [0, 2]) == [
        "https://open.spotify.com/track/1",
        "https://open.spotify.com/track/3",
    ]


def test_a_subset_is_ordered_by_position_not_by_click() -> None:
    tracks = _collection(
        _Track("a", track_id="1"),
        _Track("b", track_id="2"),
        _Track("c", track_id="3"),
    )
    assert download_target(tracks, [2, 0]) == download_target(tracks, [0, 2])


def test_selecting_nothing_downloads_nothing() -> None:
    tracks = _collection(_Track("a", track_id="1"))
    assert download_target(tracks, []) == []


def test_out_of_range_indices_are_ignored() -> None:
    tracks = _collection(_Track("a", track_id="1"))
    assert download_target(tracks, [0, 99]) == ["https://open.spotify.com/track/1"]


def test_a_linkless_track_is_dropped_from_a_subset_and_named() -> None:
    """A CSV of bare titles hits this for every row.

    Only a subset is affected: selecting the whole collection hands back its
    URL, and the provider resolves every track from that — a track without a
    link of its own is only a problem when it has to be fetched alone.
    """
    tracks = _collection(
        _Track("has a link", track_id="1"),
        _Track("bare title"),
        _Track("also linked", track_id="3"),
    )

    assert download_target(tracks, [0, 1]) == ["https://open.spotify.com/track/1"]
    assert unresolved_titles(tracks, [0, 1]) == ["bare title"]
    assert unresolved_titles(tracks, [0, 2]) == []

    assert download_target(tracks, [0, 1, 2]) == "https://open.spotify.com/album/a1"


def test_a_collection_with_no_source_url_always_lists_its_tracks() -> None:
    """A CSV has no link standing for the whole of it."""
    tracks = Tracklist(tracks=[_Track("a", track_id="1")], source_url="")
    assert download_target(tracks, [0]) == ["https://open.spotify.com/track/1"]


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def test_an_empty_url_resolves_to_nothing_without_a_request() -> None:
    result = asyncio.run(resolve_tracklist("   "))
    assert not result
    assert len(result) == 0


def test_resolving_unpacks_whatever_shape_the_provider_returns(monkeypatch) -> None:
    """Providers return 2-, 3-, or 4-tuples; all three have to work."""
    import SpotiFLAC.downloader as downloader

    shapes = {
        "two": ("Album", [_Track("a")]),
        "three": ("Album", [_Track("a")], "cover.jpg"),
        "four": ("Album", [_Track("a")], "cover.jpg", {"year": 1959}),
    }

    for name, shape in shapes.items():
        async def _get(client, url, shape=shape, **kwargs):
            return shape

        monkeypatch.setattr(downloader, "_call_metadata_get_url", _get)
        result = asyncio.run(resolve_tracklist("https://open.spotify.com/album/a"))

        assert result.name == "Album", name
        assert len(result) == 1, name
        assert result.source_url == "https://open.spotify.com/album/a"

    assert result.cover == "cover.jpg"
    assert result.meta == {"year": 1959}


def test_a_provider_failure_reaches_the_caller(monkeypatch) -> None:
    """Swallowing it here makes every failure look like an empty album."""
    import SpotiFLAC.downloader as downloader

    async def _boom(client, url, **kwargs):
        msg = "provider said no"
        raise RuntimeError(msg)

    monkeypatch.setattr(downloader, "_call_metadata_get_url", _boom)

    with pytest.raises(RuntimeError, match="provider said no"):
        asyncio.run(resolve_tracklist("https://open.spotify.com/album/a"))

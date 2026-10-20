"""Apple Music metadata parity tests for the Python client.

The fixtures model the fields used by the installed JS extension.  Keeping
these tests offline makes metadata regressions reproducible without depending
on Apple's rotating web token or catalogue contents.
"""

from __future__ import annotations

import asyncio

from mornye.core.apple_music_metadata import AppleMusicMetadataClient


def _song(song_id: str = "100", disc: int = 1) -> dict:
    """Build a song resource with rich metadata and album and artist relationships."""
    return {
        "id": song_id,
        "type": "songs",
        "attributes": {
            "name": "Example Song",
            "albumName": "Example Album",
            "artistName": "Example Artist",
            "albumArtistName": "Example Artist",
            "durationInMillis": 123456,
            "trackNumber": 2,
            "discNumber": disc,
            "releaseDate": "2024-02-03",
            "isrc": "US-ABC-24-00001",
            "composerName": "Example Composer",
            "genreNames": ["Music", "Alternative"],
            "contentRating": "explicit",
            "audioTraits": ["lossless", "atmos"],
            "hasLyrics": True,
            "hasTimeSyncedLyrics": True,
            "artwork": {"url": "https://img.test/{w}x{h}.jpg"},
            "url": "https://music.apple.com/us/song/example/100",
        },
        "relationships": {
            "albums": {"data": [{"id": "200", "type": "albums"}]},
            "artists": {
                "data": [
                    {
                        "id": "300",
                        "type": "artists",
                        "attributes": {
                            "name": "Example Artist",
                            "url": "https://music.apple.com/us/artist/example/300",
                        },
                    }
                ]
            },
        },
    }


def _album() -> dict:
    """Build an album resource with label metadata and tracks on two discs."""
    return {
        "id": "200",
        "type": "albums",
        "attributes": {
            "name": "Example Album",
            "artistName": "Example Artist",
            "releaseDate": "2024-02-03",
            "trackCount": 2,
            "recordLabel": "Example Records",
            "copyright": "© 2024 Example Records",
            "upc": "123456789012",
            "genreNames": ["Music", "Alternative"],
            "isSingle": False,
            "isCompilation": False,
            "artwork": {"url": "https://img.test/{w}x{h}.jpg"},
            "url": "https://music.apple.com/us/album/example/200",
        },
        "relationships": {
            "artists": {
                "data": [
                    {
                        "id": "300",
                        "attributes": {
                            "name": "Example Artist",
                            "url": "https://music.apple.com/us/artist/example/300",
                        },
                    }
                ]
            },
            "tracks": {
                "data": [_song("100", 1), _song("101", 2)],
            },
        },
    }


def test_parse_item_keeps_complete_apple_metadata() -> None:
    """Verify song conversion preserves album credits, identifiers, and audio traits."""
    client = AppleMusicMetadataClient()
    track = client._parse_item(_song(), _album() | {"_totalDiscs": 2})

    assert track.publisher == "Example Records"
    assert track.copyright == "© 2024 Example Records"
    assert track.composer == "Example Composer"
    assert track.genre == "Alternative"
    assert track.upc == "123456789012"
    assert track.album_id == "200"
    assert track.artist_id == "300"
    assert track.album_url.endswith("/album/example/200")
    assert track.artist_url.endswith("/artist/example/300")
    assert track.cover_url == "https://img.test/3000x3000.jpg"
    assert track.total_discs == 2
    assert track.is_explicit is True
    assert track.extra_info["apple_audio_modes"] == ["DOLBY_ATMOS"]
    assert track.extra_info["apple_has_time_synced_lyrics"] is True


def test_get_track_hydrates_album_and_caches_result() -> None:
    """Verify album hydration enriches a song and repeated reads reuse the cache."""
    client = AppleMusicMetadataClient()
    calls: list[str] = []

    async def fake_get(path: str, params=None, _media_user_token: str = "") -> dict:
        """Record requests and serve only the expected song and album fixtures."""
        calls.append(path)
        if "/songs/100" in path:
            return {"data": [_song()]}
        if "/albums/200" in path:
            return {"data": [_album()]}
        raise AssertionError(path)

    client._get = fake_get  # type: ignore[method-assign]
    first, second = asyncio.run(_get_twice(client))

    assert first.publisher == "Example Records"
    assert first.total_discs == 2
    assert first is second
    assert calls.count("/us/songs/100") == 1
    assert calls.count("/us/albums/200") == 1


async def _get_twice(client: AppleMusicMetadataClient):
    """Fetch the same track twice within one event loop."""
    first = await client.get_track("100")
    second = await client.get_track("100")
    return first, second


def test_search_async_returns_typed_tracks_and_album_fields() -> None:
    """Verify search returns enriched tracks and normalized album results."""
    client = AppleMusicMetadataClient()

    async def fake_get(path: str, params=None, _media_user_token: str = "") -> dict:
        """Serve mixed search results and the album needed for track hydration."""
        if path.endswith("/search"):
            return {
                "results": {
                    "songs": {"data": [_song()]},
                    "albums": {"data": [_album()]},
                    "artists": {"data": []},
                    "playlists": {"data": []},
                }
            }
        if "/albums/200" in path:
            return {"data": [_album()]}
        raise AssertionError(path)

    client._get = fake_get  # type: ignore[method-assign]
    result = asyncio.run(client.search_async("Example Song"))

    assert result["tracks"][0].title == "Example Song"
    assert result["tracks"][0].publisher == "Example Records"
    assert result["albums"][0]["item_type"] == "album"
    assert result["albums"][0]["label"] == "Example Records"

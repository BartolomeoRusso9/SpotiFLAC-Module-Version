import asyncio
from unittest.mock import MagicMock, patch

import pytest

from SpotiFLAC.core.isrc_finder import IsrcFinder, _normalize_isrc, spotify_id_to_gid


def test_spotify_id_to_gid_accepts_spotify_url():
    assert spotify_id_to_gid("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT") == "4cOdK2wGLETKBW3PvgPWqT"


def test_spotify_id_to_gid_accepts_spotify_uri():
    assert spotify_id_to_gid("spotify:track:4cOdK2wGLETKBW3PvgPWqT") == "4cOdK2wGLETKBW3PvgPWqT"


def test_spotify_id_to_gid_rejects_invalid_id():
    with pytest.raises(ValueError):
        spotify_id_to_gid("not-a-track")


def test_normalize_isrc_returns_uppercase_valid_isrc():
    assert _normalize_isrc("us-rc1-23-12345") == "USRC12312345"


def test_normalize_isrc_returns_none_for_invalid_isrc():
    assert _normalize_isrc("invalid-isrc") is None


def test_find_isrc_async_returns_none_when_access_tokens_missing():
    finder = IsrcFinder(http_client=None)
    mock_client = MagicMock(
        access_token=None,
        client_token=None,
        initialize=MagicMock(),
        get_isrc_from_metadata=MagicMock(return_value="USRC12312345"),
    )

    with patch("SpotiFLAC.core.spotfetch.SpotifyWebClient", return_value=mock_client):
        result = asyncio.run(finder.find_isrc_async("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"))

    assert result is None


def test_find_isrc_async_normalizes_isrc_from_spotify_metadata():
    finder = IsrcFinder(http_client=None)
    mock_client = MagicMock(
        access_token="token",
        client_token="token",
        initialize=MagicMock(),
        get_isrc_from_metadata=MagicMock(return_value="us-rc1-23-12345"),
    )

    with patch("SpotiFLAC.core.spotfetch.SpotifyWebClient", return_value=mock_client):
        result = asyncio.run(finder.find_isrc_async("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"))
    assert result == "USRC12312345"

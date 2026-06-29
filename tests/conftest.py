"""
Shared fixtures and helpers for SpotiFLAC v1.2.8 tests.
"""

import pytest
from unittest.mock import MagicMock, patch

# ─── Sample URLs ─────────────────────────────────────────────────────────────

SPOTIFY_TRACK = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
SPOTIFY_ALBUM = "https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ"
SPOTIFY_PLAYLIST = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
SPOTIFY_ARTIST = "https://open.spotify.com/artist/06HL4z0CvFAxyc27GXpf02"

TIDAL_ALBUM = "https://listen.tidal.com/album/364272512"
TIDAL_TRACK = "https://listen.tidal.com/track/12345678"

APPLE_MUSIC_ALBUM = "https://music.apple.com/us/album/folklore/1528309825"
SOUNDCLOUD_TRACK = "https://soundcloud.com/artist/track-name"
YOUTUBE_VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PANDORA_TRACK = "https://www.pandora.com/artist/name/album/track"

INVALID_URL = "https://example.com/not-a-music-url"
GARBAGE_STRING = "not a url at all"


# ─── Valid services ───────────────────────────────────────────────────────────

ALL_SERVICES = ["tidal", "qobuz", "deezer", "amazon"]
DOWNLOAD_ONLY = ["joox", "netease", "migu", "kuwo"]  # cannot be input URLs


# ─── Quality constants ────────────────────────────────────────────────────────

TIDAL_QUALITIES = ["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"]
QOBUZ_QUALITIES = ["6", "7", "27"]
APPLE_QUALITIES = ["alac", "atmos", "ac3", "aac", "aac-legacy"]
PANDORA_QUALITIES = ["mp3_192", "aac_64", "aac_32"]


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_output_dir(tmp_path):
    """A temporary directory that acts as the download destination."""
    out = tmp_path / "downloads"
    out.mkdir()
    return str(out)


@pytest.fixture
def mock_spotiflac():
    """
    Returns a MagicMock that mimics the SpotiFLAC class so tests can
    verify argument handling without making real network calls.
    """
    with patch("backend.SpotiFLAC") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield mock_cls, instance


@pytest.fixture
def mock_download_success(mock_spotiflac):
    """SpotiFLAC instance that always reports successful downloads."""
    mock_cls, instance = mock_spotiflac
    instance.download.return_value = {
        "success": True,
        "downloaded": 1,
        "failed": 0,
    }
    return mock_cls, instance


@pytest.fixture
def mock_download_failure(mock_spotiflac):
    """SpotiFLAC instance that always reports failed downloads."""
    mock_cls, instance = mock_spotiflac
    instance.download.return_value = {
        "success": False,
        "downloaded": 0,
        "failed": 1,
    }
    return mock_cls, instance

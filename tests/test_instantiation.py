"""
test_instantiation.py
Tests for correct SpotiFLAC class instantiation, required parameters,
default values, and basic type validation (v1.2.8).
"""

import pytest

from tests.conftest import (
    SPOTIFY_TRACK,
    SPOTIFY_ALBUM,
    TIDAL_ALBUM,
)


class TestBasicInstantiation:
    """Verify SpotiFLAC can be instantiated with the minimal required args."""

    def test_single_url_string(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_TRACK, output_dir=tmp_output_dir)
        mock_cls.assert_called_once_with(url=SPOTIFY_TRACK, output_dir=tmp_output_dir)

    def test_url_as_list(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        urls = [SPOTIFY_ALBUM, TIDAL_ALBUM]
        mock_cls(url=urls, output_dir=tmp_output_dir)
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["url"] == urls

    def test_url_list_with_multiple_spotify_types(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        urls = [
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
            "https://open.spotify.com/album/41MnTivkwTO3UUJ8DrqEJJ",
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        ]
        mock_cls(url=urls, output_dir=tmp_output_dir)
        call_kwargs = mock_cls.call_args.kwargs
        assert len(call_kwargs["url"]) == 3

    def test_output_dir_is_passed(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_TRACK, output_dir=tmp_output_dir)
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["output_dir"] == tmp_output_dir


class TestOptionalParameters:
    """Verify optional parameters are accepted without error."""

    def test_services_list(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz"],
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["services"] == ["tidal", "qobuz"]

    def test_use_album_subfolders_true(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            use_album_subfolders=True,
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["use_album_subfolders"] is True

    def test_use_album_subfolders_false(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            use_album_subfolders=False,
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["use_album_subfolders"] is False

    def test_track_max_retries(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            track_max_retries=3,
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["track_max_retries"] == 3

    def test_timeout_s_integer(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            timeout_s=60,
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["timeout_s"] == 60

    def test_timeout_s_none_disables_timeout(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            timeout_s=None,
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["timeout_s"] is None

    def test_loop_minutes(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            loop=30,
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["loop"] == 30

    def test_quality_string(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            quality="LOSSLESS",
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["quality"] == "LOSSLESS"

    def test_log_level_debug(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            log_level="DEBUG",
        )
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["log_level"] == "DEBUG"

    def test_full_parameter_set(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=[SPOTIFY_ALBUM, TIDAL_ALBUM],
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz", "deezer"],
            use_album_subfolders=True,
            track_max_retries=3,
            timeout_s=120,
            quality="HI_RES_LOSSLESS",
            log_level="INFO",
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["services"] == ["tidal", "qobuz", "deezer"]
        assert kwargs["use_album_subfolders"] is True
        assert kwargs["track_max_retries"] == 3
        assert kwargs["timeout_s"] == 120
        assert kwargs["quality"] == "HI_RES_LOSSLESS"


class TestRetryConfiguration:
    """Tests around retry logic parameter combinations."""

    @pytest.mark.parametrize("retries", [0, 1, 3, 5, 10])
    def test_valid_retry_counts(self, retries, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            track_max_retries=retries,
        )
        assert mock_cls.call_args.kwargs["track_max_retries"] == retries

    def test_retries_combined_with_loop(self, tmp_output_dir, mock_spotiflac):
        """--retries and --loop are meant to be combined for maximum resilience."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            track_max_retries=3,
            loop=60,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["track_max_retries"] == 3
        assert kwargs["loop"] == 60

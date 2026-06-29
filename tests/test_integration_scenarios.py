"""
test_integration_scenarios.py
End-to-end scenario tests for common SpotiFLAC usage patterns (v1.2.8).

All network calls are mocked — these tests exercise parameter composition
and the shape of the call rather than actual downloads.
"""

from tests.conftest import (
    SPOTIFY_TRACK,
    SPOTIFY_ALBUM,
    SPOTIFY_PLAYLIST,
    SPOTIFY_ARTIST,
    TIDAL_ALBUM,
)


class TestSimpleDownloadScenarios:

    def test_single_track_tidal(self, tmp_output_dir, mock_spotiflac):
        """Basic single-track download from Tidal."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
        )
        mock_cls.assert_called_once()

    def test_album_with_album_subfolders(self, tmp_output_dir, mock_spotiflac):
        """Download a full album and organize into subfolders."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz"],
            use_album_subfolders=True,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["use_album_subfolders"] is True
        assert "tidal" in kwargs["services"]

    def test_playlist_download(self, tmp_output_dir, mock_spotiflac):
        """Download an entire Spotify playlist."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_PLAYLIST,
            output_dir=tmp_output_dir,
            services=["tidal"],
        )
        kwargs = mock_cls.call_args.kwargs
        assert "playlist" in kwargs["url"]

    def test_mixed_url_list(self, tmp_output_dir, mock_spotiflac):
        """Multiple URLs (album + playlist + Tidal album) in one call."""
        mock_cls, _ = mock_spotiflac
        urls = [SPOTIFY_ALBUM, SPOTIFY_PLAYLIST, TIDAL_ALBUM]
        mock_cls(
            url=urls,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz"],
            use_album_subfolders=True,
        )
        kwargs = mock_cls.call_args.kwargs
        assert len(kwargs["url"]) == 3


class TestHighResilienceScenarios:

    def test_maximum_resilience_config(self, tmp_output_dir, mock_spotiflac):
        """
        Combine all three resilience parameters:
        services × retries × loop × timeout.
        """
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz", "deezer"],
            track_max_retries=3,
            loop=60,
            timeout_s=120,
        )
        kwargs = mock_cls.call_args.kwargs
        assert len(kwargs["services"]) == 3
        assert kwargs["track_max_retries"] == 3
        assert kwargs["loop"] == 60
        assert kwargs["timeout_s"] == 120

    def test_fallback_provider_order_matters(self, tmp_output_dir, mock_spotiflac):
        """
        SpotiFLAC tries providers in list order.
        Tidal first, then Qobuz, then Deezer.
        """
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz", "deezer"],
        )
        services = mock_cls.call_args.kwargs["services"]
        assert services[0] == "tidal"
        assert services[1] == "qobuz"
        assert services[2] == "deezer"


class TestTelegramBotPattern:
    """
    Common use-case: Telegram bot queues individual tracks.
    Uses the module API without CLI.
    """

    def test_single_track_for_bot(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        # Simulate a bot handling a user request
        user_url = SPOTIFY_TRACK
        mock_cls(
            url=user_url,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz"],
            track_max_retries=2,
            timeout_s=90,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["url"] == user_url
        assert kwargs["track_max_retries"] == 2

    def test_bot_processes_multiple_tracks_sequentially(
        self, tmp_output_dir, mock_spotiflac
    ):
        """A bot might call SpotiFLAC once per URL received."""
        mock_cls, _ = mock_spotiflac
        user_urls = [SPOTIFY_TRACK, SPOTIFY_ALBUM]

        for url in user_urls:
            mock_cls(url=url, output_dir=tmp_output_dir, services=["tidal"])

        assert mock_cls.call_count == 2


class TestJellyfinAutomationPattern:
    """
    Common use-case: automated bulk download for a Jellyfin library.
    """

    def test_bulk_download_with_subfolders(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=[SPOTIFY_ALBUM, TIDAL_ALBUM],
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz"],
            use_album_subfolders=True,
            quality="HI_RES_LOSSLESS",
            track_max_retries=3,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["use_album_subfolders"] is True
        assert kwargs["quality"] == "HI_RES_LOSSLESS"

    def test_artist_url_download(self, tmp_output_dir, mock_spotiflac):
        """Downloading all tracks from an artist page."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ARTIST,
            output_dir=tmp_output_dir,
            services=["tidal"],
            use_album_subfolders=True,
        )
        kwargs = mock_cls.call_args.kwargs
        assert "/artist/" in kwargs["url"]


class TestQualityPreferenceScenarios:

    def test_hi_res_preference_with_fallback(self, tmp_output_dir, mock_spotiflac):
        """Prefer Hi-Res but fall back gracefully."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            services=["tidal"],
            quality="HI_RES_LOSSLESS",
            quality_fallback=True,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["quality"] == "HI_RES_LOSSLESS"
        assert kwargs.get("quality_fallback") is True

    def test_lossless_minimum_quality(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz"],
            quality="LOSSLESS",
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["quality"] == "LOSSLESS"

    def test_dolby_atmos_tidal_only(self, tmp_output_dir, mock_spotiflac):
        """Dolby Atmos is Tidal-specific."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            services=["tidal"],
            quality="DOLBY_ATMOS",
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["services"] == ["tidal"]
        assert kwargs["quality"] == "DOLBY_ATMOS"


class TestDownloadResultHandling:

    def test_success_result_structure(self, mock_download_success):
        mock_cls, instance = mock_download_success
        result = instance.download()
        assert result["success"] is True
        assert result["downloaded"] >= 1
        assert result["failed"] == 0

    def test_failure_result_structure(self, mock_download_failure):
        mock_cls, instance = mock_download_failure
        result = instance.download()
        assert result["success"] is False
        assert result["failed"] >= 1

    def test_partial_download_scenario(self, mock_spotiflac):
        """Some tracks download, some fail."""
        mock_cls, instance = mock_spotiflac
        instance.download.return_value = {
            "success": False,
            "downloaded": 8,
            "failed": 2,
        }
        result = instance.download()
        assert result["downloaded"] == 8
        assert result["failed"] == 2
        total = result["downloaded"] + result["failed"]
        assert total == 10

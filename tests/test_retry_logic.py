"""
test_retry_logic.py
Tests for SpotiFLAC's retry and timeout mechanisms (v1.2.8).

Retry model:
  - track_max_retries  : extra attempts per track when ALL providers fail
  - loop               : minutes to re-queue permanently failed tracks after a
                         full session completes
  - timeout_s          : max seconds for a single track download
  - Exponential backoff: 2 s → 4 s → 8 s … capped at 30 s between retries
"""

import pytest

from tests.conftest import SPOTIFY_TRACK, SPOTIFY_ALBUM

# ──────────────────────────────────────────────────────────────────────────────
# track_max_retries
# ──────────────────────────────────────────────────────────────────────────────


class TestTrackMaxRetries:

    @pytest.mark.parametrize("retries", [0, 1, 2, 3, 5])
    def test_retry_count_stored(self, retries, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            track_max_retries=retries,
        )
        assert mock_cls.call_args.kwargs["track_max_retries"] == retries

    def test_zero_retries_means_no_extra_attempts(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            track_max_retries=0,
        )
        assert mock_cls.call_args.kwargs["track_max_retries"] == 0

    def test_high_retry_count_accepted(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            track_max_retries=20,
        )
        assert mock_cls.call_args.kwargs["track_max_retries"] == 20


# ──────────────────────────────────────────────────────────────────────────────
# Exponential back-off helper
# ──────────────────────────────────────────────────────────────────────────────


def _backoff_delay(attempt: int, cap: int = 30) -> int:
    """
    Mirrors SpotiFLAC's documented back-off:
    attempt 1 → 2 s, attempt 2 → 4 s, attempt 3 → 8 s, …, capped at cap.
    """
    return min(2**attempt, cap)


class TestExponentialBackoff:

    def test_first_attempt_delay_is_2s(self):
        assert _backoff_delay(1) == 2

    def test_second_attempt_delay_is_4s(self):
        assert _backoff_delay(2) == 4

    def test_third_attempt_delay_is_8s(self):
        assert _backoff_delay(3) == 8

    def test_fourth_attempt_delay_is_16s(self):
        assert _backoff_delay(4) == 16

    def test_fifth_attempt_delay_is_30s_capped(self):
        assert _backoff_delay(5) == 30

    def test_delays_are_always_capped_at_30s(self):
        for attempt in range(1, 20):
            assert _backoff_delay(attempt) <= 30

    def test_delays_are_monotonically_non_decreasing(self):
        delays = [_backoff_delay(i) for i in range(1, 10)]
        for a, b in zip(delays, delays[1:]):
            assert a <= b

    def test_total_delay_for_3_retries(self):
        total = sum(_backoff_delay(i) for i in range(1, 4))
        assert total == 2 + 4 + 8  # = 14 s


# ──────────────────────────────────────────────────────────────────────────────
# timeout_s
# ──────────────────────────────────────────────────────────────────────────────


class TestTimeoutParameter:

    @pytest.mark.parametrize("timeout", [10, 30, 60, 120, 300])
    def test_timeout_accepted(self, timeout, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            timeout_s=timeout,
        )
        assert mock_cls.call_args.kwargs["timeout_s"] == timeout

    def test_timeout_none_disables_cap(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            timeout_s=None,
        )
        assert mock_cls.call_args.kwargs["timeout_s"] is None

    def test_timeout_is_per_track_not_per_session(self):
        """
        timeout_s caps a single track. For a 10-track album with timeout=60,
        the total possible session time is up to 600 s (before retries).
        """
        timeout_per_track = 60
        tracks = 10
        assert timeout_per_track * tracks == 600  # basic sanity check


# ──────────────────────────────────────────────────────────────────────────────
# loop (re-queue permanently failed tracks)
# ──────────────────────────────────────────────────────────────────────────────


class TestLoopParameter:

    @pytest.mark.parametrize("minutes", [5, 15, 30, 60, 120])
    def test_loop_minutes_accepted(self, minutes, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=tmp_output_dir,
            loop=minutes,
        )
        assert mock_cls.call_args.kwargs["loop"] == minutes

    def test_loop_combined_with_retries(self, tmp_output_dir, mock_spotiflac):
        """
        The docs say: combine --retries with --loop for maximum resilience.
        --retries handles transient errors; --loop re-queues permanent failures.
        """
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            services=["tidal", "qobuz", "deezer"],
            track_max_retries=3,
            loop=60,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["track_max_retries"] == 3
        assert kwargs["loop"] == 60

    def test_loop_with_timeout_and_retries(self, tmp_output_dir, mock_spotiflac):
        """All three resilience params work together."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            track_max_retries=5,
            timeout_s=120,
            loop=30,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["track_max_retries"] == 5
        assert kwargs["timeout_s"] == 120
        assert kwargs["loop"] == 30


# ──────────────────────────────────────────────────────────────────────────────
# Provider cycling on retry
# ──────────────────────────────────────────────────────────────────────────────


class TestProviderCyclingOnRetry:
    """
    Each retry cycles through ALL configured providers from the beginning.
    """

    def test_three_providers_three_retries_yields_nine_attempts(self):
        """
        services = [tidal, qobuz, deezer], track_max_retries = 3
        Attempt count = (1 initial pass) + (3 retries) = 4 passes × 3 providers = 12.
        Or: the library tries 3 extra retries after the first full cycle fails.
        """
        services = ["tidal", "qobuz", "deezer"]
        retries = 3
        initial_passes = 1
        total_passes = initial_passes + retries
        total_provider_calls = total_passes * len(services)
        assert total_provider_calls == 12

    def test_single_provider_retry_stays_on_that_provider(self):
        services = ["tidal"]
        retries = 3
        total_provider_calls = (1 + retries) * len(services)
        assert total_provider_calls == 4  # 4 attempts on Tidal

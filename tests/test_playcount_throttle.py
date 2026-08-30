"""Guards the politeness of the playcount column.

Playcounts have no bulk query — it is one request per track against
Spotify's internal GraphQL API, and a CSV import can mean thousands of
them. SpotifyWebClient.query() has no 429 handling of its own, so the two
things that keep that from becoming a burst are the interval in
_fetch_track_playcounts() and the pause while a download is running. Both
are easy to drop by accident when that method is next touched, and neither
fails loudly when it goes: the requests just get faster.
"""

from __future__ import annotations

import threading
import time

from SpotiFLAC.app import (
    PLAYCOUNT_MIN_INTERVAL_S,
    PLAYCOUNT_WORKERS,
    SpotiFLAC_API,
)


class _RecordingClient:
    """Stands in for SpotifyWebClient, noting when each request starts."""

    def __init__(self, latency: float = 0.0) -> None:
        self.starts: list[float] = []
        self._latency = latency
        self._lock = threading.Lock()

    def get_track_stats(self, track_id: str) -> dict:
        with self._lock:
            self.starts.append(time.monotonic())
        if self._latency:
            time.sleep(self._latency)
        return {"playcount": "42", "rank": "", "status": ""}


def _api() -> SpotiFLAC_API:
    api = SpotiFLAC_API()
    api.log = lambda *a, **k: None  # type: ignore[method-assign]
    return api


def test_requests_are_spaced_by_the_configured_interval() -> None:
    """No two request *starts* may be closer than the interval, however many
    workers are running — the pool size must not set the request rate.
    """
    api = _api()
    client = _RecordingClient()
    ids = [f"id{i:022d}" for i in range(24)]

    stats = api._fetch_track_playcounts(client, ids)

    assert len(stats) == len(ids)
    starts = sorted(client.starts)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # A small tolerance: time.sleep() may wake fractionally early.
    assert min(gaps) >= PLAYCOUNT_MIN_INTERVAL_S - 0.01, (
        f"requests started {min(gaps):.3f}s apart, floor is "
        f"{PLAYCOUNT_MIN_INTERVAL_S}s — the throttle is not being applied"
    )


def test_ids_are_deduplicated_before_being_requested() -> None:
    """The same track twice in a CSV must not cost two lookups."""
    api = _api()
    client = _RecordingClient()

    api._fetch_track_playcounts(client, ["a", "b", "a", "", "b", "a"])

    assert len(client.starts) == 2


def test_fetch_pauses_while_a_download_is_running() -> None:
    """A download owns the connection; the playcount column waits for it."""
    api = _api()
    client = _RecordingClient()
    ids = [f"id{i:022d}" for i in range(40)]

    hold_for = 1.5
    api._download_active.set()

    def _release() -> None:
        time.sleep(hold_for)
        api._download_active.clear()

    releaser = threading.Thread(target=_release)
    releaser.start()
    started = time.monotonic()
    stats = api._fetch_track_playcounts(client, ids)
    releaser.join()

    assert len(stats) == len(ids), "work must resume, not be dropped"
    first_request = min(client.starts) - started
    assert first_request >= hold_for - 0.1, (
        f"first request went out {first_request:.2f}s in, before the download "
        "finished — the pause is not being honoured"
    )


def test_pool_size_stays_small() -> None:
    """A guard on the constant itself: the point is a trickle, not a burst."""
    assert 1 <= PLAYCOUNT_WORKERS <= 4
    assert PLAYCOUNT_MIN_INTERVAL_S >= 0.05


def test_download_task_always_clears_the_active_flag() -> None:
    """A flag left set would starve the playcount fetch forever, so it is
    cleared in _download_task's `finally` — including on the early-return
    and exception paths.
    """
    api = _api()
    api.set_progress = lambda *a, **k: None  # type: ignore[method-assign]
    api._push = lambda *a, **k: None  # type: ignore[method-assign]
    api._push_download_stats = lambda *a, **k: None  # type: ignore[method-assign]

    # Early return: no services selected.
    api._download_task([], {"services": []})
    assert not api._download_active.is_set()

    # Exception path: an index that isn't in current_tracks.
    api.current_tracks = []
    api._download_task([0], {"services": ["tidal"], "log_level": "INFO"})
    assert not api._download_active.is_set()

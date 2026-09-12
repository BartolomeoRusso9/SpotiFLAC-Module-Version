"""core/subscription_scheduler.py — the clock, without a network.

The check itself is injected, so these only pin the timing: what is due gets
checked, what is not does not, and a check that blows up is not retried on
every poll.
"""

from __future__ import annotations

import threading

from SpotiFLAC.core import subscriptions as subs
from SpotiFLAC.core.subscription_scheduler import SubscriptionScheduler

PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
ARTIST_URL = "https://open.spotify.com/artist/0000000000000000000000"


def _checking(seen: list):
    def run_check(sub) -> None:
        seen.append(sub.id)
        subs.record_check(sub.id)  # what check_async() does

    return run_check


def test_tick_checks_what_is_due_and_nothing_else():
    scheduled = subs.add(PLAYLIST_URL, interval_minutes=15)
    subs.add(ARTIST_URL)  # manual
    seen: list[str] = []
    scheduler = SubscriptionScheduler(_checking(seen))

    assert scheduler.tick() == 1
    assert seen == [scheduled.id]
    # Checked a moment ago: not due again until its interval runs out.
    assert scheduler.tick() == 0


def test_a_check_that_raises_is_stamped_not_retried_every_poll():
    sub = subs.add(PLAYLIST_URL, interval_minutes=15)

    def boom(_sub) -> None:
        raise RuntimeError("no network")

    scheduler = SubscriptionScheduler(boom)

    assert scheduler.tick() == 1
    assert subs.get(sub.id).last_error == "no network"
    assert scheduler.tick() == 0


def test_it_runs_on_its_own_thread_until_stopped():
    subs.add(PLAYLIST_URL, interval_minutes=15)
    ran = threading.Event()
    seen: list[str] = []
    check = _checking(seen)

    def run_check(sub) -> None:
        check(sub)
        ran.set()

    scheduler = SubscriptionScheduler(run_check, poll_seconds=0.01)
    scheduler.start()
    try:
        assert ran.wait(5)
    finally:
        scheduler.stop()
    assert len(seen) == 1


def test_stop_keeps_the_worker_while_a_check_is_still_running():
    """A check that outlives stop()'s timeout must not be joined by a second
    worker: start() checks self._thread, so clearing it early would let two
    threads check the same subscriptions at once."""
    subs.add(PLAYLIST_URL, interval_minutes=15)
    started, release = threading.Event(), threading.Event()

    def slow_check(sub) -> None:
        subs.record_check(sub.id)
        started.set()
        release.wait(10)

    scheduler = SubscriptionScheduler(slow_check, poll_seconds=0.01)
    scheduler.start()
    try:
        assert started.wait(5)
        worker = scheduler._thread

        scheduler.stop(timeout=0.1)  # the check is still running
        assert scheduler._thread is worker

        scheduler.start()  # must not start a second one
        assert scheduler._thread is worker
    finally:
        release.set()
        scheduler.stop(timeout=5)
    assert scheduler._thread is None

"""core/subscription_scheduler.py — checking subscriptions on their own timer.

A subscription with `interval_minutes` > 0 is checked whenever that much time
has passed since its last check (see subscriptions.due()); what the check
finds new is downloaded. This module is only the clock: *what* a check does —
list, compare, queue the downloads — is the `run_check` callable it is given,
which the GUI (app.py) and the web server (webapp.py) wire to the Api
instance that owns the subscription. So the timing is testable without a
network, a provider or a window.

One thread, one subscription at a time. Checks are several Spotify calls
each, and running them side by side is how an instance gets rate-limited;
the downloads they queue run on the ordinary download path either way.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from . import subscriptions
from .subscriptions import Subscription

logger = logging.getLogger(__name__)

#: How often the scheduler looks for due subscriptions. The shortest interval
#: a subscription can have is subscriptions.MIN_INTERVAL_MINUTES, so a check
#: runs at most this late.
POLL_SECONDS = 60.0


class SubscriptionScheduler:
    def __init__(
        self,
        run_check: Callable[[Subscription], None],
        *,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._run_check = run_check
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, now: float | None = None) -> int:
        """Checks every subscription that is due; returns how many."""
        checked = 0
        for sub in subscriptions.due(now):
            if self._stop.is_set():
                break
            try:
                self._run_check(sub)
            except Exception as exc:
                logger.exception(
                    "[subscriptions] Scheduled check of %s failed",
                    sub.name or sub.url,
                )
                # Stamped here too: a check that raised before reaching
                # check_async() would otherwise still be due, and be retried
                # on every poll until whatever broke it is fixed.
                subscriptions.record_check(sub.id, str(exc))
            checked += 1
        return checked

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                self.tick()
            except Exception:
                # The store itself failing (a locked or missing database) must
                # not end the thread: nothing would ever restart it.
                logger.exception("[subscriptions] Scheduler poll failed")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="subscription-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

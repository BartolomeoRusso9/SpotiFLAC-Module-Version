"""Tests for --watch's repeat loop (SpotiFLAC.launcher.watch_forever)."""

from __future__ import annotations

import asyncio
import inspect

from SpotiFLAC.launcher import watch_forever


class _StopWatching(Exception):
    """Sentinel used to break out of watch_forever's infinite loop in tests."""


def test_watch_forever_reruns_and_sleeps_the_right_amount() -> None:
    calls = 0
    sleep_calls: list[float] = []

    async def run_once() -> None:
        nonlocal calls
        calls += 1

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 3:
            raise _StopWatching

    async def _run() -> None:
        await watch_forever(run_once, minutes=5, sleep=fake_sleep)

    try:
        asyncio.run(_run())
        raised = False
    except _StopWatching:
        raised = True

    assert raised, "watch_forever returned instead of looping forever"
    # Each cycle is "sleep, then re-run": fake_sleep raises on its 3rd call,
    # before that 3rd cycle's run_once() — so 3 sleeps but only 2 re-runs.
    assert calls == 2
    assert sleep_calls == [300, 300, 300]


def test_watch_forever_uses_real_asyncio_sleep_by_default() -> None:
    """The default `sleep` parameter really is asyncio.sleep, not a stub —
    guards against a refactor quietly changing what --watch sleeps with.
    """
    sig = inspect.signature(watch_forever)
    assert sig.parameters["sleep"].default is asyncio.sleep

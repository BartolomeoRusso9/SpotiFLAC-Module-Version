"""Progress output must stay readable in a log file.

A tqdm bar redraws itself with carriage returns. On a terminal that is a
moving bar; in a log it is escape soup, and `docker logs` collapses each
refresh into a "[285B blob data]" line that buries everything else. These
tests pin the two halves of the fix: no bars without a terminal, and
throttled plain-text progress in their place.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from contextlib import contextmanager

import pytest

from SpotiFLAC.core import progress as progress_module
from SpotiFLAC.core.progress import (
    ProgressManager,
    install_console_interception,
    progress_bars_enabled,
    uninstall_console_interception,
)


@pytest.fixture(autouse=True)
def _clean_progress_state():
    def reset() -> None:
        ProgressManager._progress_log_state.clear()
        # The queue is a class attribute bound to whichever loop created it;
        # each test runs its own asyncio.run(), so it has to start fresh.
        ProgressManager._event_queue = None
        ProgressManager._worker_task = None

    reset()
    yield
    reset()


@contextmanager
def isolated_root_logger():
    """Root logger carrying only the handlers the test installs on it.

    Has to be entered from the test body rather than from a fixture: pytest
    attaches its own capture handler to the root logger for the call phase,
    i.e. after fixture setup has already run.
    """
    root = logging.getLogger()
    original = list(root.handlers)
    root.handlers.clear()
    try:
        yield root
    finally:
        root.handlers.clear()
        root.handlers.extend(original)


class _FakeStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# ─── Bar suppression ────────────────────────────────────────────────────────


def test_bars_off_without_a_terminal(monkeypatch):
    monkeypatch.delenv("SPOTIFLAC_PROGRESS_BARS", raising=False)
    monkeypatch.setattr(sys, "__stderr__", _FakeStream(tty=False))
    assert progress_bars_enabled() is False


def test_bars_on_with_a_terminal(monkeypatch):
    monkeypatch.delenv("SPOTIFLAC_PROGRESS_BARS", raising=False)
    monkeypatch.setattr(sys, "__stderr__", _FakeStream(tty=True))
    assert progress_bars_enabled() is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("on", True), ("0", False), ("no", False)],
)
def test_env_var_overrides_terminal_detection(monkeypatch, value, expected):
    # Forced against what the terminal says, in both directions.
    monkeypatch.setattr(sys, "__stderr__", _FakeStream(tty=not expected))
    monkeypatch.setenv("SPOTIFLAC_PROGRESS_BARS", value)
    assert progress_bars_enabled() is expected


def test_created_bar_is_disabled_without_a_terminal(monkeypatch):
    monkeypatch.delenv("SPOTIFLAC_PROGRESS_BARS", raising=False)
    monkeypatch.setattr(sys, "__stderr__", _FakeStream(tty=False))
    bar = ProgressManager.create_bar("item", "Some Song", 1000)
    try:
        assert bar.disable is True
    finally:
        ProgressManager.release_bar("item")


# ─── Textual replacement ────────────────────────────────────────────────────


def _drain(events: list[tuple[int, int]], monkeypatch) -> str:
    """Feeds progress events with bars off and returns what reached stderr."""
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)

    async def run() -> None:
        ProgressManager.start_worker()
        for current, total in events:
            ProgressManager.enqueue_progress("item", "Some Song", current, total)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        await ProgressManager.stop_worker()

    asyncio.run(run())
    return captured.getvalue()


def test_progress_without_bars_emits_no_carriage_returns(monkeypatch):
    monkeypatch.setenv("SPOTIFLAC_PROGRESS_BARS", "0")
    total = 30_000_000
    output = _drain([(n, total) for n in range(0, total + 1, 500_000)], monkeypatch)

    assert "\r" not in output
    assert "Some Song" in output


def test_progress_lines_are_throttled(monkeypatch):
    """A whole download costs a handful of lines, not one per chunk."""
    monkeypatch.setenv("SPOTIFLAC_PROGRESS_BARS", "0")
    total = 30_000_000
    # 60 chunks arriving back to back: the time gate alone should cut it to one.
    output = _drain([(n, total) for n in range(0, total + 1, 500_000)], monkeypatch)

    assert len([line for line in output.splitlines() if line.strip()]) <= 2


def test_no_progress_line_for_a_finished_track(monkeypatch):
    """The result line already reports the finished download."""
    monkeypatch.setenv("SPOTIFLAC_PROGRESS_BARS", "0")
    output = _drain([(1000, 1000)], monkeypatch)

    assert output.strip() == ""
    assert "item" not in ProgressManager._progress_log_state


def test_unknown_total_produces_no_percentage(monkeypatch):
    monkeypatch.setenv("SPOTIFLAC_PROGRESS_BARS", "0")
    output = _drain([(500, None), (1500, None)], monkeypatch)

    assert output.strip() == ""


# ─── Log handler bookkeeping ────────────────────────────────────────────────


def test_console_interception_restores_the_host_handlers():
    """A long-lived host runs one download per job — its logging must survive."""
    with isolated_root_logger() as root:
        handler = logging.StreamHandler(io.StringIO())
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        root.addHandler(handler)
        before = list(root.handlers)

        try:
            for _ in range(3):
                install_console_interception()
                uninstall_console_interception()
            assert list(root.handlers) == before
        finally:
            uninstall_console_interception()


def test_console_interception_is_idempotent():
    """Installing twice must not double every log line."""
    with isolated_root_logger() as root:
        try:
            install_console_interception()
            install_console_interception()
            installed = [
                h
                for h in root.handlers
                if isinstance(h, progress_module.TqdmLoggingHandler)
            ]
            assert len(installed) == 1
        finally:
            uninstall_console_interception()


def test_console_interception_keeps_the_host_formatter():
    with isolated_root_logger() as root:
        handler = logging.StreamHandler(io.StringIO())
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        root.addHandler(handler)

        try:
            install_console_interception()
            installed = next(
                h
                for h in root.handlers
                if isinstance(h, progress_module.TqdmLoggingHandler)
            )
            assert "asctime" in installed.formatter._fmt
        finally:
            uninstall_console_interception()

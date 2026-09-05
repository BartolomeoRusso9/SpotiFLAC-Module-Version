"""The output sink: with one installed, a run must not touch the terminal.

That is the whole contract Phase 0 of the TUI work buys, and it is worth
pinning down precisely, because every way it can break is silent — a single
line escaping onto the real stderr does not fail anything, it just tears
somebody's frame. So the assertions here are made at the file-descriptor
level (``capfd``, not ``capsys``): the interception proxy deliberately writes
to ``sys.__stdout__``, which a ``sys.stdout`` swap would not catch.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from SpotiFLAC.core import console, output_sink, progress
from SpotiFLAC.core.output_sink import (
    CallbackLogHandler,
    CallbackSink,
    emit,
    output_sink as output_sink_ctx,
    set_output_sink,
    sink_active,
)
from SpotiFLAC.core.progress import ProgressManager, progress_bars_enabled


class RecordingSink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_line(self, line: str, stream: str = "stdout") -> None:
        self.lines.append((line, stream))

    @property
    def text(self) -> str:
        return "\n".join(line for line, _ in self.lines)


@pytest.fixture(autouse=True)
def _no_leftover_sink():
    """No test may leak a sink into the next one — it would mute it."""
    yield
    output_sink.clear_output_sink()


def test_no_sink_writes_to_the_terminal(capfd) -> None:
    emit("plain line")
    assert not sink_active()
    assert "plain line" in capfd.readouterr().out


def test_sink_receives_the_line_and_the_terminal_stays_clean(capfd) -> None:
    sink = RecordingSink()
    with output_sink_ctx(sink):
        emit("captured", stream="stderr")

    assert sink.lines == [("captured", "stderr")]
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_context_manager_restores_the_previous_sink() -> None:
    outer, inner = RecordingSink(), RecordingSink()
    with output_sink_ctx(outer):
        with output_sink_ctx(inner):
            emit("inner")
        emit("outer")

    assert inner.text == "inner"
    assert outer.text == "outer"
    assert not sink_active()


def test_a_plain_callable_is_accepted_as_a_sink() -> None:
    seen: list[str] = []
    with output_sink_ctx(seen.append):
        emit("one-arg callback")
    assert seen == ["one-arg callback"]


def test_callback_sink_passes_the_stream_when_the_callback_takes_it() -> None:
    seen: list[tuple[str, str]] = []
    sink = CallbackSink(lambda line, stream: seen.append((line, stream)))
    sink.write_line("line", "stderr")
    assert seen == [("line", "stderr")]


def test_a_failing_sink_does_not_propagate(capfd) -> None:
    def explode(line: str, stream: str) -> None:
        msg = "sink is broken"
        raise RuntimeError(msg)

    with output_sink_ctx(explode):
        emit("swallowed")

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_console_helpers_route_through_the_sink(capfd) -> None:
    sink = RecordingSink()
    with output_sink_ctx(sink):
        console.print_run_header(3, ["tidal"], "FLAC", "/tmp/out", 2)
        console.print_track_done("tidal", "Song", "flac", 1024 * 1024, 4.0)
        console.print_summary(1, 1, 0, [], 4.0)

    assert "[RUN] 3 track(s)" in sink.text
    assert "SESSION SUMMARY" in sink.text
    assert all(stream == "stderr" for _, stream in sink.lines)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_safe_writers_route_through_the_sink(capfd) -> None:
    sink = RecordingSink()
    with output_sink_ctx(sink):
        progress.safe_print("hello", "world")
        progress.safe_tqdm_write("aside")

    assert sink.text == "hello world\naside"
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_sink_outranks_the_progress_bars_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SPOTIFLAC_PROGRESS_BARS", "1")
    assert progress_bars_enabled() is True

    with output_sink_ctx(RecordingSink()):
        assert progress_bars_enabled() is False

    assert progress_bars_enabled() is True


def test_no_tqdm_bar_is_built_while_a_sink_is_installed() -> None:
    with output_sink_ctx(RecordingSink()):
        assert ProgressManager.create_bar("item-1", "Track", 1000) is None
        assert "item-1" not in ProgressManager._bars

        ProgressManager.initialize_master_bar(10, description="Progress")
        assert ProgressManager._master_bar is None
        assert ProgressManager._master_enabled is False

        # Still callable, still a no-op: the run does not have to know.
        ProgressManager.increment_master()
        ProgressManager.clear_master_bar()


def test_a_simulated_download_writes_nothing_to_the_terminal(capfd) -> None:
    """The Phase 0 acceptance test, end to end.

    Everything a real run emits — console lines, the downloader's asides,
    log records, per-track progress, and a bare ``print()`` from some
    library that never heard of any of this — goes out while a sink is
    installed, and the terminal must come back empty.
    """
    sink = RecordingSink()

    async def scenario() -> None:
        progress.install_console_interception()
        try:
            console.print_run_header(2, ["deezer"], "FLAC", "/tmp/out", 1)
            console.print_track_header(1, 2, "Song", "Artist", "Album")
            progress.safe_tqdm_write("  merging segments...")
            logging.getLogger("SpotiFLAC.test").error("something went wrong")
            print("a stray print nobody routed")

            ProgressManager.initialize_master_bar(2, description="Progress")
            for received in (250_000, 500_000, 750_000):
                ProgressManager.enqueue_progress("id-1", "Song", received, 1_000_000)
            # Let the consumer task drain the queue it was just handed.
            await asyncio.sleep(0.05)
            ProgressManager.increment_master()
        finally:
            await ProgressManager.clear_all()
            progress.uninstall_console_interception()

    with output_sink_ctx(sink):
        asyncio.run(scenario())

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    text = sink.text
    assert "[RUN] 2 track(s)" in text
    assert "merging segments" in text
    assert "something went wrong" in text
    assert "a stray print nobody routed" in text


def test_callback_log_handler_buckets_severity() -> None:
    seen: list[tuple[str, str]] = []
    handler = CallbackLogHandler(lambda msg, severity: seen.append((msg, severity)))
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("SpotiFLAC.test.callback")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
    finally:
        logger.removeHandler(handler)

    assert seen == [("d", "debug"), ("i", "debug"), ("w", "warn"), ("e", "error")]


def test_callback_log_handler_swallows_a_broken_callback() -> None:
    def explode(message: str, severity: str) -> None:
        msg = "ui is gone"
        raise RuntimeError(msg)

    handler = CallbackLogHandler(explode)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(
        "SpotiFLAC.test", logging.ERROR, __file__, 1, "boom", None, None,
    )
    handler.emit(record)  # must not raise


def test_set_output_sink_returns_the_previous_one() -> None:
    first, second = RecordingSink(), RecordingSink()
    assert set_output_sink(first) is None
    assert set_output_sink(second) is first
    assert set_output_sink(None) is second
    assert not sink_active()

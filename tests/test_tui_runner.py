"""The TUI's download path, with a fake run underneath it.

Two claims are worth pinning here, and both are about the seam rather than
the download:

* progress reaches the UI as **structured events** from
  `DownloadBroadcaster`, not as parsed console output — the thing that makes
  a terminal UI over an existing CLI feasible at all;
* while the run is on, **nothing reaches the terminal**. The Phase 0 sink is
  what buys that, and this is where it is checked against a run rather than
  against a synthetic call.

The download itself is replaced: `launcher.run_download_from_cfg` is the
seam, and driving `DownloadManager` by hand produces exactly the events a
real run produces, in a tenth of a second and with no network.
"""

from __future__ import annotations

import asyncio
import functools
import logging

import pytest

import SpotiFLAC.launcher as launcher_module
from SpotiFLAC.core import output_sink
from SpotiFLAC.core.console import print_run_header, print_track_done
from SpotiFLAC.core.progress import DownloadManager, safe_tqdm_write
from SpotiFLAC.tui.app import SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState
from SpotiFLAC.tui.runner import (
    FAILED,
    FINISHED,
    OUTPUT,
    STATS,
    DownloadRunner,
    make_status_line,
)


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def _clean_singletons():
    """The queue is a process-wide singleton; a test must not inherit one."""
    asyncio.run(DownloadManager().reset())
    yield
    asyncio.run(DownloadManager().reset())
    output_sink.clear_output_sink()


async def _fake_download(cfg: dict, log_level: int) -> None:
    """One track, start to finish, through the real progress plumbing."""
    manager = DownloadManager()
    print_run_header(1, cfg["services"], cfg["quality"], cfg["output_dir"], 1)

    await manager.add_to_queue("t1", "Fake Song", "Fake Artist", "Fake Album", "sp1")
    await manager.start_download("t1")
    safe_tqdm_write("  fetching from the provider...")
    logging.getLogger("SpotiFLAC.test.fake").warning("a warning nobody should see")
    for received in (2.0, 6.0, 10.0):
        await manager.update_progress("t1", received, 10.0, 3.5)
    await manager.complete_download("t1", "/tmp/fake.flac", 10.0)
    print_track_done("tidal", "Fake Song", "flac", 10 * 1024 * 1024, 3.0)


def _ready_cfg() -> dict:
    return ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/spotiflac-test",
        services=["tidal"],
    ).to_cfg()


@drives_the_ui
async def test_a_run_reports_progress_and_leaves_the_terminal_alone(
    monkeypatch,
    capfd,
) -> None:
    monkeypatch.setattr(launcher_module, "run_download_from_cfg", _fake_download)

    runner = DownloadRunner(_ready_cfg(), logging.INFO)
    kinds: list[str] = []
    lines: list[str] = []
    last_stats: dict = {}

    async for kind, payload, _severity in runner.events():
        kinds.append(kind)
        if kind == OUTPUT:
            lines.append(str(payload))
        elif kind == STATS:
            last_stats = payload

    captured = capfd.readouterr()
    assert captured.out == "", "a run under the TUI must not write to stdout"
    assert captured.err == "", "a run under the TUI must not write to stderr"

    assert STATS in kinds, "no structured progress reached the UI"
    assert FINISHED in kinds

    text = "\n".join(lines)
    assert "[RUN] 1 track(s)" in text
    assert "fetching from the provider" in text
    assert "a warning nobody should see" in text

    assert last_stats["completed"] == 1
    assert last_stats["downloads"][0]["track_name"] == "Fake Song"
    assert runner.outcome.ok
    assert runner.outcome.completed == 1


@drives_the_ui
async def test_the_sink_is_removed_once_the_run_ends(monkeypatch, capfd) -> None:
    """An interrupted or finished run must give the terminal back."""
    monkeypatch.setattr(launcher_module, "run_download_from_cfg", _fake_download)

    runner = DownloadRunner(_ready_cfg(), logging.INFO)
    async for _kind, _payload, _severity in runner.events():
        assert output_sink.sink_active(), "output escaped during the run"

    assert not output_sink.sink_active()
    capfd.readouterr()


@drives_the_ui
async def test_a_failing_run_is_reported_rather_than_raised(monkeypatch, capfd) -> None:
    async def _explode(cfg: dict, log_level: int) -> None:
        msg = "the provider said no"
        raise RuntimeError(msg)

    monkeypatch.setattr(launcher_module, "run_download_from_cfg", _explode)

    runner = DownloadRunner(_ready_cfg(), logging.INFO)
    kinds = [kind async for kind, _payload, _severity in runner.events()]

    assert FAILED in kinds
    assert FINISHED not in kinds
    assert runner.outcome.ok is False
    assert "the provider said no" in runner.outcome.error

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@drives_the_ui
async def test_cancelling_the_iteration_restores_the_terminal(
    monkeypatch,
    capfd,
) -> None:
    async def _forever(cfg: dict, log_level: int) -> None:
        while True:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(launcher_module, "run_download_from_cfg", _forever)

    runner = DownloadRunner(_ready_cfg(), logging.INFO)
    events = runner.events()

    async def _drain() -> None:
        async for _event in events:
            pass

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await events.aclose()

    assert not output_sink.sink_active()
    capfd.readouterr()


@drives_the_ui
async def test_log_records_arrive_even_when_the_package_logger_is_detached(
    monkeypatch,
    capfd,
) -> None:
    """The production case, not a corner one.

    `_run_download_async` installs its own console handler on the "SpotiFLAC"
    logger and sets `propagate = False` — right for a terminal run, and it
    used to leave the TUI's log pane empty, because a handler on root never
    saw those records again. The runner attaches to both loggers for exactly
    this.
    """
    package_logger = logging.getLogger("SpotiFLAC")
    propagate, level = package_logger.propagate, package_logger.level
    package_logger.propagate = False
    try:
        monkeypatch.setattr(launcher_module, "run_download_from_cfg", _fake_download)

        runner = DownloadRunner(_ready_cfg(), logging.INFO)
        lines = [
            str(payload)
            async for kind, payload, _severity in runner.events()
            if kind == OUTPUT
        ]
    finally:
        package_logger.propagate = propagate
        package_logger.setLevel(level)

    assert any("a warning nobody should see" in line for line in lines)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@drives_the_ui
async def test_a_log_line_is_not_delivered_twice(monkeypatch, capfd) -> None:
    """Attaching to two loggers must not double every record."""
    monkeypatch.setattr(launcher_module, "run_download_from_cfg", _fake_download)

    runner = DownloadRunner(_ready_cfg(), logging.INFO)
    lines = [
        str(payload)
        async for kind, payload, _severity in runner.events()
        if kind == OUTPUT
    ]
    capfd.readouterr()

    warnings = [line for line in lines if "a warning nobody should see" in line]
    assert len(warnings) == 1


def test_a_bar_never_contradicts_its_badge() -> None:
    """A full bar means finished; a moving one means moving.

    Both were wrong before: a failed track drew a full bar, which reads as a
    success, and a queued one drew the indeterminate pulse, which reads as
    busy. The badge says what happened — the bar must not say otherwise.
    """
    from SpotiFLAC.tui.queue_view import TrackRow

    class _Bar:
        def __init__(self):
            self.calls = []

        def update(self, **kwargs):
            self.calls.append(kwargs)

    def _applied(status: str, **extra) -> dict:
        row = TrackRow({"id": "t", "track_name": "x", "status": status})
        row._bar = _Bar()
        row._label = None
        row._badge = None
        row.apply({"id": "t", "track_name": "x", "status": status, **extra})
        return row._bar.calls[-1]

    assert _applied("completed") == {"total": 1.0, "progress": 1.0}
    assert _applied("failed") == {"total": 1.0, "progress": 0.0}
    assert _applied("skipped") == {"total": 1.0, "progress": 0.0}
    assert _applied("queued") == {"total": 1.0, "progress": 0.0}
    assert _applied("downloading", total_size=40.0, progress=12.0) == {
        "total": 40.0,
        "progress": 12.0,
    }
    # Only a track actually being fetched, with no size yet, may pulse.
    assert _applied("downloading") == {"total": None}


def test_the_status_line_says_what_is_happening() -> None:
    line = make_status_line(
        {
            "completed": 3,
            "queued": 2,
            "failed": 1,
            "skipped": 0,
            "current_speed": 4.25,
            "total_downloaded": 128.0,
        },
    )
    assert "3 done" in line
    assert "2 queued" in line
    assert "1 failed" in line
    assert "4.2 MB/s" in line
    assert "skipped" not in line, "a zero count is noise, not information"


# ---------------------------------------------------------------------------
# The queue panel, fed the same events
# ---------------------------------------------------------------------------


@drives_the_ui
async def test_the_queue_panel_fills_from_broadcaster_events(monkeypatch) -> None:
    monkeypatch.setattr(launcher_module, "run_download_from_cfg", _fake_download)

    state = ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/spotiflac-test",
        services=["tidal"],
    )

    async with SpotiFLACTui(state).run_test() as pilot:
        pilot.app.action_start_download()

        for _ in range(200):
            await pilot.pause()
            if not pilot.app._download_running:
                break

        from SpotiFLAC.tui.queue_view import QueuePanel

        queue = pilot.app.query_one("#queue", QueuePanel)
        assert len(queue._rows) == 1

        row = next(iter(queue._rows.values()))
        assert "Fake Song" in str(row._label.content)
        # The outcome lives in the badge, MovieBox-style: a short label on a
        # solid colour, next to the title rather than crammed into it.
        assert "DONE" in str(row._badge.content)
        assert row._badge.has_class("badge-success")
        assert row.has_class("completed")

        # The run's console output ends up in the log pane, which the run
        # itself reveals — there is nowhere else for it to go.
        assert pilot.app.query_one("#log-pane").display is True
        assert "downloaded" in str(pilot.app.query_one("#status").content)

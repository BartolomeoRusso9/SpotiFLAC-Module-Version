"""runner.py — Running a download while something else owns the screen.

Three things have to happen at once for the TUI to show a live download, and
each of them already exists somewhere in the codebase; this module is the
wiring, not the machinery.

* **Nothing may reach the terminal.** The output sink from `core.output_sink`
  takes every console line, every `safe_print`, and — via a
  `CallbackLogHandler` — every log record, and hands them here instead.
  Progress bars switch themselves off as a side effect of the sink being
  installed, so no tqdm ever draws over the layout.
* **Progress has to arrive as data.** `DownloadBroadcaster` already emits
  structured dicts for the GUI; the TUI subscribes to the same queue. No
  parsing of bar output, which is the thing that usually makes a terminal UI
  over an existing CLI unpleasant.
* **The run itself is unchanged.** `launcher.run_download_from_cfg` is the
  same call the wizard makes, with the same dict.

Everything crossing back into the UI goes through one asyncio queue, filled
with `call_soon_threadsafe`. Parts of a download legitimately run in worker
threads (`asyncio.to_thread` around blocking provider code), so a callback
that touched a widget directly would eventually do it from the wrong thread —
rarely, and only under load, which is the worst way to find out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.output_sink import CallbackLogHandler, set_output_sink
from ..core.progress import DownloadBroadcaster, DownloadManager

#: What a UI event carries: ("output", line, severity) or ("stats", dict, "").
Event = tuple[str, Any, str]

OUTPUT = "output"
STATS = "stats"
FINISHED = "finished"
FAILED = "failed"


@dataclass
class RunOutcome:
    """How a run ended, for the UI to report without re-deriving it."""

    completed: int = 0
    failed: int = 0
    skipped: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class DownloadRunner:
    """One download run, with its output redirected into an asyncio queue.

    The UI creates one of these per run, iterates :meth:`events` until it
    stops, and reads :attr:`outcome` afterwards. Cancelling the task that
    iterates is enough to stop everything: the sink is removed and the log
    handler detached in a `finally`, so an interrupted run cannot leave the
    process mute.
    """

    def __init__(self, cfg: dict, log_level: int = logging.INFO) -> None:
        self._cfg = cfg
        self._log_level = log_level
        self._events: asyncio.Queue[Event | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.outcome = RunOutcome()

    # ------------------------------------------------------------------
    # Producing
    # ------------------------------------------------------------------

    def _push(self, kind: str, payload: Any, severity: str = "") -> None:
        """Queues one event from wherever it was produced, thread or not."""
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._events.put_nowait, (kind, payload, severity))

    def _on_line(self, line: str, stream: str = "stdout") -> None:
        self._push(OUTPUT, line, "warn" if stream == "stderr" else "")

    def _on_log(self, message: str, severity: str = "debug") -> None:
        self._push(OUTPUT, message, severity)

    async def _pump_progress(self, queue: asyncio.Queue) -> None:
        """Forwards broadcaster stats into the same stream as the output.

        One stream rather than two, so the UI never has to reason about the
        order a progress update and the line explaining it arrived in.
        """
        while True:
            stats = await queue.get()
            self._push(STATS, stats)

    # ------------------------------------------------------------------
    # Consuming
    # ------------------------------------------------------------------

    async def events(self):
        """Runs the download, yielding UI events until it ends.

        An async generator rather than callbacks because the consumer is a
        Textual worker: it wants to `async for` over the run and stop by
        being cancelled, and callbacks would need their own teardown path.
        """
        self._loop = asyncio.get_running_loop()

        progress_queue: asyncio.Queue = asyncio.Queue()
        broadcaster = DownloadBroadcaster()
        await broadcaster.subscribe(progress_queue)

        handler = CallbackLogHandler(self._on_log)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        handler.setLevel(self._log_level)

        # Both loggers, and the level with them. The root handler alone is
        # not enough: a CLI run sets `propagate = False` on "SpotiFLAC" so
        # its own console handler is the only one — correct for a terminal,
        # and it would leave this pane silent. Restoring the level matters
        # too, since the pane is the only place these records now go and the
        # default root level would drop everything below WARNING.
        loggers = self._loggers_to_attach()
        restore_levels = [(logger, logger.level) for logger in loggers]

        previous_sink = set_output_sink(self._on_line)
        for logger in loggers:
            logger.addHandler(handler)
            if logger.level == logging.NOTSET or logger.level > self._log_level:
                logger.setLevel(self._log_level)
        pump = asyncio.create_task(self._pump_progress(progress_queue))
        run = asyncio.create_task(self._run())

        try:
            while True:
                event = await self._events.get()
                if event is None:
                    break
                yield event
        finally:
            # Order matters on the way out: stop feeding the queue, then give
            # the terminal back. A line emitted after the sink is gone lands
            # on the real stdout, underneath the UI still holding the screen.
            pump.cancel()
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run
            for logger in loggers:
                logger.removeHandler(handler)
            for logger, level in restore_levels:
                logger.setLevel(level)
            set_output_sink(previous_sink)
            await broadcaster.unsubscribe(progress_queue)

    @staticmethod
    def _loggers_to_attach() -> list[logging.Logger]:
        """Root, plus "SpotiFLAC" when it does not propagate to root."""
        root = logging.getLogger()
        package = logging.getLogger("SpotiFLAC")
        if package.propagate:
            # One handler on root already sees everything; a second would
            # print every line twice.
            return [root]
        return [root, package]

    async def _run(self) -> None:
        # Imported here, not at module scope: launcher.py pulls in the whole
        # download stack, and merely opening the TUI should not pay for it.
        from ..launcher import run_download_from_cfg

        try:
            await run_download_from_cfg(self._cfg, self._log_level)
        except asyncio.CancelledError:
            raise
        except SystemExit as exc:
            self.outcome.error = f"the run exited early ({exc.code})"
        except Exception as exc:
            self.outcome.error = f"{type(exc).__name__}: {exc}"
            self._push(OUTPUT, f"Download failed — {exc}", "error")
        finally:
            with contextlib.suppress(Exception):
                stats = await DownloadManager().get_stats()
                self.outcome.completed = int(stats.get("completed", 0))
                self.outcome.failed = int(stats.get("failed", 0))
                self.outcome.skipped = int(stats.get("skipped", 0))
                self._push(STATS, stats)
            self._push(FINISHED if self.outcome.ok else FAILED, self.outcome)
            # Sentinel: unblocks the consumer's `await`, ending the iteration.
            with contextlib.suppress(RuntimeError):
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._events.put_nowait, None)


def make_status_line(stats: dict) -> str:
    """A one-line summary of a broadcaster stats dict, for a status bar."""
    speed = float(stats.get("current_speed", 0.0) or 0.0)
    parts = [
        f"{int(stats.get('completed', 0))} done",
        f"{int(stats.get('queued', 0))} queued",
    ]
    if stats.get("failed"):
        parts.append(f"{int(stats['failed'])} failed")
    if stats.get("skipped"):
        parts.append(f"{int(stats['skipped'])} skipped")
    if speed > 0:
        parts.append(f"{speed:.1f} MB/s")
    total = float(stats.get("total_downloaded", 0.0) or 0.0)
    if total > 0:
        parts.append(f"{total:.1f} MB")
    return "  ·  ".join(parts)


#: Callback signature the widgets use, kept here so both sides agree on it.
EventHandler = Callable[[str, Any, str], None]

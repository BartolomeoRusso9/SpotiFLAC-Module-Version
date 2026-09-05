"""output_sink.py — One switchable destination for everything a run prints.

Every human-readable line a download produces goes out through one of three
doors: ``core/console`` (the run header, per-track lines, the summary
banner), ``safe_print``/``safe_tqdm_write`` in ``core/progress`` (the
downloader's own asides), and the logging handler installed by
``install_console_interception``. All three write straight to stdout/stderr
via ``tqdm.write``, which is exactly right for a terminal and exactly wrong
for a full-screen UI: a single stray line lands in the middle of someone
else's layout and the frame is torn until the next full redraw.

So instead of teaching each call site about the caller, they all funnel
through :func:`emit`, which asks one module-level question — is a sink
installed? — and either hands the line to the sink or falls through to the
current ``tqdm.write`` behaviour. With no sink installed nothing changes for
the CLI, which is the point: this module is meant to be invisible until
something claims the screen.

Installing a sink also switches off the tqdm bars (see
``progress_bars_enabled``), because a bar is a stream of carriage returns
aimed at a terminal the sink's owner is now drawing on.

Typical use, from a UI that owns the screen::

    with output_sink(lambda line, stream: log_widget.write(line)):
        await run_download(...)
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import IO, Protocol, runtime_checkable

# Imported lazily inside _write_through_tqdm so that merely importing this
# module — which core/console does, at import time — does not drag tqdm in.
STDOUT = "stdout"
STDERR = "stderr"


@runtime_checkable
class OutputSink(Protocol):
    """Anything that can accept a line of console output.

    ``stream`` is the door the line would have gone out of ("stdout" or
    "stderr") rather than an instruction: a sink is free to render both the
    same way, and most do.
    """

    def write_line(self, line: str, stream: str = STDOUT) -> None: ...


class CallbackSink:
    """Adapts a plain callable into an :class:`OutputSink`.

    The callback is invoked as ``callback(line, stream)``; a one-argument
    callable is accepted too, since most callers only care about the text.
    """

    def __init__(self, callback: Callable[..., None]) -> None:
        self._callback = callback

    def write_line(self, line: str, stream: str = STDOUT) -> None:
        try:
            self._callback(line, stream)
        except TypeError:
            self._callback(line)


_sink: OutputSink | None = None


def set_output_sink(sink: OutputSink | Callable[..., None] | None) -> OutputSink | None:
    """Installs *sink* and returns whatever was installed before it.

    Returning the previous sink rather than nothing is what makes nesting
    work: a caller that has to install one by hand (a test, a host that
    cannot use the context manager) can put the old one back afterwards
    instead of unconditionally clearing and stepping on its own caller.
    """
    global _sink
    previous = _sink
    if sink is None:
        _sink = None
    elif isinstance(sink, OutputSink):
        _sink = sink
    else:
        _sink = CallbackSink(sink)
    return previous


def clear_output_sink() -> None:
    global _sink
    _sink = None


def get_output_sink() -> OutputSink | None:
    return _sink


def sink_active() -> bool:
    """Whether console output is currently being captured.

    Read by ``progress_bars_enabled()`` and by ``ProgressManager``: with a
    sink installed no tqdm bar is created at all, since a bar writes to the
    real stderr regardless of where the surrounding lines are going.
    """
    return _sink is not None


@contextmanager
def output_sink(sink: OutputSink | Callable[..., None]) -> Iterator[OutputSink | None]:
    """Installs *sink* for the duration of the block, then restores."""
    previous = set_output_sink(sink)
    try:
        yield previous
    finally:
        set_output_sink(previous)


def emit(line: str, *, stream: str = STDOUT, file: IO[str] | None = None) -> None:
    """Writes one line to the sink, or to the terminal when there is none.

    ``file`` is only consulted on the fall-through path; a sink is told the
    logical ``stream`` instead, because the file object a caller happened to
    pass says nothing useful to a UI that has no file to write to.
    """
    current = _sink
    if current is not None:
        with contextlib.suppress(Exception):
            current.write_line(line, stream)
        return
    _write_through_tqdm(line, stream=stream, file=file)


def _write_through_tqdm(
    line: str,
    *,
    stream: str = STDOUT,
    file: IO[str] | None = None,
) -> None:
    import sys

    from tqdm import tqdm

    target = file
    if target is None:
        target = sys.stderr if stream == STDERR else sys.stdout
    with tqdm.get_lock():
        tqdm.write(line, file=target)


class CallbackLogHandler(logging.Handler):
    """Mirrors log records into a callback instead of a stream.

    Generalised out of the GUI's ``UILogHandler``, which did exactly this
    for the Logs panel: format the record, hand the text plus a coarse
    severity to the UI, and never let a failure in the UI propagate back
    into the code that logged. The severity is reported as one of
    ``"error"``/``"warn"``/``"debug"`` — the same three buckets the GUI
    panel uses, and enough for a UI to decide whether a line deserves the
    user's attention or only the log pane.
    """

    def __init__(self, callback: Callable[..., None]) -> None:
        super().__init__()
        self._callback = callback

    @staticmethod
    def severity_for(levelno: int) -> str:
        if levelno >= logging.ERROR:
            return "error"
        if levelno >= logging.WARNING:
            return "warn"
        return "debug"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            severity = self.severity_for(record.levelno)
            try:
                self._callback(message, severity)
            except TypeError:
                self._callback(message)
        except Exception:
            # A UI that blows up mid-render must not turn every subsequent
            # log call into a second exception on top of the first.
            pass

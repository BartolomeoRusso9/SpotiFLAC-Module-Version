"""window_size.py — Opens the TUI in a window worth drawing it in.

The TUI is laid out for a wide terminal: a sidebar of modes down the left,
the panel you are working in next to it, and the log as a third column
beside that. In the 80x24 a terminal opens at by default none of that fits —
the log drops back under the panel (`app.SpotiFLACTui._TWO_COLUMN_MIN_WIDTH`),
the queue is a column of ellipses, and the log shows two lines. Nothing is
broken, it just looks like a much worse program than it is, and the person
seeing it has no reason to guess that dragging the window corner is the
fix.

So ask. `CSI 8 ; rows ; cols t` is the XTerm window-manipulation sequence for
"resize the text area", and it is understood by every terminal this UI is
worth opening in — Apple Terminal, iTerm2, WezTerm, Ghostty, kitty, and the
VS Code terminal. Somewhere it is not understood it is swallowed silently,
which is exactly the right failure: the sequence draws nothing, so an
unsupported terminal is left with the small window it already had.

Three rules keep the asking polite:

* **Only grow.** Someone running full-screen on a large display already has
  more than this asks for, and shrinking them to fit a number in this file
  would be worse than doing nothing.
* **Only put it back if it is still ours.** The size is restored on exit,
  but only when the terminal is still exactly the size that was asked for.
  Resize the window during a run and that resize is yours to keep.
* **Only when there is a window.** A pipe, a multiplexer pane, `TERM=dumb`
  or `SPOTIFLAC_TUI_NO_RESIZE=1` all mean no, and are checked before a byte
  is written.

Covered by tests/test_tui_window_size.py.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
from collections.abc import Iterator

#: What to ask for. Comfortably past the width the log needs to become a
#: column of its own rather than a strip under the panel, with enough left
#: over that neither the queue's track titles nor the log's file paths spend
#: their lives wrapped; and tall enough that the log column is worth having.
TARGET_COLUMNS = 180
TARGET_ROWS = 54

#: How long to wait for the terminal to act on the request. The resize is
#: asynchronous — the sequence goes out, the window manager gets to it when
#: it gets to it — and Textual measures the screen as it starts, so a first
#: frame drawn before the new size lands is laid out for the old one. Short
#: enough not to be felt, generous enough for a terminal that repaints.
_SETTLE_TIMEOUT = 0.5
_SETTLE_STEP = 0.02

#: Set to anything non-empty to be left alone.
_OPT_OUT = "SPOTIFLAC_TUI_NO_RESIZE"


def _resizable() -> bool:
    """Whether writing a resize request here could do anything but harm."""
    if os.environ.get(_OPT_OUT):
        return False
    term = os.environ.get("TERM", "")
    if not term or term == "dumb":
        return False
    # A multiplexer owns its panes: the sequence either does nothing or
    # resizes the wrong thing, and the pane cannot outgrow its window anyway.
    if os.environ.get("TMUX") or term.startswith(("screen", "tmux")):
        return False
    try:
        return sys.__stdout__ is not None and sys.__stdout__.isatty()
    except Exception:
        return False


def _current() -> tuple[int, int] | None:
    """(columns, rows) as the terminal reports them, or None if it will not."""
    try:
        size = shutil.get_terminal_size()
    except Exception:
        return None
    if size.columns <= 0 or size.lines <= 0:
        return None
    return size.columns, size.lines


def _request(columns: int, rows: int) -> None:
    stream = sys.__stdout__
    if stream is None:
        return
    with contextlib.suppress(Exception):
        stream.write(f"\x1b[8;{rows};{columns}t")
        stream.flush()


def _settle(target: tuple[int, int]) -> None:
    """Waits for the reported size to catch up with what was asked for."""
    deadline = time.monotonic() + _SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        if _current() == target:
            return
        time.sleep(_SETTLE_STEP)


def enlarge() -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Grows the window towards the target, before the first frame.

    Returns ``(original, requested)`` when a request went out, so the caller
    can put the first one back; ``None`` when nothing was asked for, either
    because this is not a resizable terminal or because it is already at
    least as large as the target.
    """
    if not _resizable():
        return None
    original = _current()
    if original is None:
        return None
    requested = (
        max(original[0], TARGET_COLUMNS),
        max(original[1], TARGET_ROWS),
    )
    if requested == original:
        return None
    _request(*requested)
    _settle(requested)
    return original, requested


def restore(state: tuple[tuple[int, int], tuple[int, int]] | None) -> None:
    """Puts the window back, unless the user has since resized it themselves."""
    if state is None:
        return
    original, requested = state
    if _current() != requested:
        return
    _request(*original)


@contextlib.contextmanager
def enlarged_window() -> Iterator[None]:
    """Runs the block in a window grown to the target size."""
    state = enlarge()
    try:
        yield
    finally:
        restore(state)

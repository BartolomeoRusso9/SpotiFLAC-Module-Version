"""Growing the terminal window before the TUI draws its first frame.

The interesting cases are all refusals. Writing an escape sequence at a
terminal is cheap and silent, so the risk is not that it fails — it is that
it succeeds somewhere it should not have been sent: shrinking a full-screen
window to fit a constant in this file, resizing a tmux pane, or writing
bytes into a pipe someone is reading the output of.

The restore is the other half: a run leaves the window as it found it, but
only when it is still the size the run asked for. A user who drags the
corner mid-run has said what size they want, and that outranks us.
"""

from __future__ import annotations


import pytest

from SpotiFLAC.tui import window_size


class _FakeStdout:
    def __init__(self, tty: bool = True) -> None:
        self.written: list[str] = []
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass


@pytest.fixture
def terminal(monkeypatch):
    """A resizable terminal of a given size, recording what is written to it.

    `size` is what `_current()` reports; assigning to it is how a test says
    "the terminal did what it was told" (or refused to).
    """

    state = {"size": (80, 24)}
    stdout = _FakeStdout()

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv(window_size._OPT_OUT, raising=False)
    monkeypatch.setattr(window_size.sys, "__stdout__", stdout)
    monkeypatch.setattr(window_size, "_current", lambda: state["size"])
    # The settle loop would otherwise spend its full timeout waiting for a
    # size nothing in the test is going to change.
    monkeypatch.setattr(window_size, "_SETTLE_TIMEOUT", 0.0)

    stdout.state = state
    return stdout


def _resize_requests(stdout) -> list[tuple[int, int]]:
    """(columns, rows) for every `CSI 8 ; rows ; cols t` that went out."""
    out = []
    for text in stdout.written:
        assert text.startswith("\x1b[8;") and text.endswith("t")
        rows, columns = text[4:-1].split(";")
        out.append((int(columns), int(rows)))
    return out


def test_a_default_window_is_grown_to_the_target(terminal) -> None:
    window_size.enlarge()

    assert _resize_requests(terminal) == [
        (window_size.TARGET_COLUMNS, window_size.TARGET_ROWS)
    ]


def test_a_window_already_larger_is_left_alone(terminal) -> None:
    terminal.state["size"] = (
        window_size.TARGET_COLUMNS + 40,
        window_size.TARGET_ROWS + 10,
    )

    assert window_size.enlarge() is None
    assert terminal.written == []


def test_only_the_short_dimension_grows(terminal) -> None:
    """A wide but short window keeps its width and gains rows."""
    terminal.state["size"] = (window_size.TARGET_COLUMNS + 40, 24)

    window_size.enlarge()

    assert _resize_requests(terminal) == [
        (window_size.TARGET_COLUMNS + 40, window_size.TARGET_ROWS)
    ]


@pytest.mark.parametrize(
    "env",
    [
        {"TERM": "dumb"},
        {"TERM": ""},
        {"TERM": "screen-256color"},
        {"TMUX": "/tmp/tmux-501/default,1,0"},
        {window_size._OPT_OUT: "1"},
    ],
    ids=["dumb", "no-term", "screen", "tmux", "opt-out"],
)
def test_nothing_is_written_where_a_resize_makes_no_sense(
    terminal, monkeypatch, env
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert window_size.enlarge() is None
    assert terminal.written == []


def test_nothing_is_written_into_a_pipe(terminal, monkeypatch) -> None:
    monkeypatch.setattr(window_size.sys, "__stdout__", _FakeStdout(tty=False))

    assert window_size.enlarge() is None
    assert terminal.written == []


def test_the_window_is_put_back_on_the_way_out(terminal) -> None:
    with window_size.enlarged_window():
        # The terminal obeyed, so the size on screen is still ours to undo.
        terminal.state["size"] = (
            window_size.TARGET_COLUMNS,
            window_size.TARGET_ROWS,
        )

    assert _resize_requests(terminal)[-1] == (80, 24)


def test_a_resize_by_the_user_outranks_the_restore(terminal) -> None:
    with window_size.enlarged_window():
        terminal.state["size"] = (200, 60)

    assert _resize_requests(terminal) == [
        (window_size.TARGET_COLUMNS, window_size.TARGET_ROWS)
    ]


def test_restore_after_a_refusal_writes_nothing(terminal) -> None:
    window_size.restore(None)

    assert terminal.written == []

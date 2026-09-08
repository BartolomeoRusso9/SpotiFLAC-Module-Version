"""A set DISPLAY is not a running X server.

The `--web` container never started Xvfb — its entrypoint skips the display
setup for web mode — while the image sets `DISPLAY=:99` for every mode. The
bootstrap read the variable, concluded a display existed and returned, so
every Turnstile solve spent Chromium's full 45s start timeout reaching for a
display nobody had started, and reported `FailedToStartBrowser` next to
`binary_exists=True`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import tempfile

import pytest

from SpotiFLAC.core import solver

# The probe reaches an X server over an AF_UNIX socket, which Windows has no
# notion of — CPython does not even define socket.AF_UNIX there. The two tests
# that stand a real socket up are POSIX-only; the rest of the file exercises
# the parsing, which is platform-agnostic, and runs everywhere.
needs_unix_sockets = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="no AF_UNIX on this platform, so no X11 socket to probe",
)


@pytest.fixture
def x11_socket_dir(monkeypatch):
    """A stand-in for /tmp/.X11-unix the probe can be pointed at.

    Under /tmp rather than pytest's tmp_path: an AF_UNIX path is capped at
    ~104 bytes and the usual tmp_path (/var/folders/... on macOS) blows
    through it.
    """
    directory = pathlib.Path(tempfile.mkdtemp(prefix="x11-", dir="/tmp"))
    monkeypatch.setattr(solver, "_X11_SOCKET_DIR", str(directory))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _listening(path) -> socket.socket:
    """An X server, as far as the probe is concerned: something that accepts."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    # Backlog well over 1: the probe connects and never accepts, so a
    # one-deep queue fills on the first call and the second probe of the same
    # display is refused — a property of this stub, not of an X server.
    server.listen(64)
    return server


def test_an_unset_display_is_not_live() -> None:
    assert solver._display_is_live("") is False
    assert solver._display_is_live("   ") is False


@needs_unix_sockets
def test_a_local_display_with_nobody_listening_is_not_live(x11_socket_dir) -> None:
    """The Docker case: DISPLAY=:99, and no Xvfb behind it."""
    assert solver._display_is_live(":99") is False


@needs_unix_sockets
def test_a_display_someone_answers_on_is_live(x11_socket_dir) -> None:
    server = _listening(x11_socket_dir / "X7")
    try:
        assert solver._display_is_live(":7") is True
        assert solver._display_is_live(":7.0") is True
    finally:
        server.close()


def test_a_remote_display_is_taken_at_its_word() -> None:
    """Not ours to probe, and definitely not ours to start Xvfb over."""
    assert solver._display_is_live("somehost:0") is True
    assert solver._display_is_live("localhost:10.0") is True


def test_a_dead_display_starts_one_instead_of_returning(monkeypatch) -> None:
    """The bug itself: DISPLAY set, nothing listening, no Xvfb started."""
    monkeypatch.setattr(solver.platform, "system", lambda: "Linux")
    monkeypatch.setattr(solver, "_xvfb_started", False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(solver, "_display_is_live", lambda display: False)

    started: list[str] = []
    monkeypatch.setattr(
        solver,
        "_start_xvfb_if_needed",
        lambda: started.append(os.environ.get("DISPLAY", "")),
    )

    solver._ensure_xvfb()

    assert started == [":99"], "a dead DISPLAY must not pass for a live one"


def test_a_live_display_is_left_alone(monkeypatch) -> None:
    monkeypatch.setattr(solver.platform, "system", lambda: "Linux")
    monkeypatch.setattr(solver, "_xvfb_started", False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(solver, "_display_is_live", lambda display: True)

    started: list[str] = []
    monkeypatch.setattr(
        solver, "_start_xvfb_if_needed", lambda: started.append("started")
    )

    solver._ensure_xvfb()

    assert started == []

"""A download started from the TUI died on its first progress bar.

    ValueError: bad value(s) in fds_to_keep

The first `tqdm.get_lock()` of a process builds tqdm's multiprocessing lock,
and creating any multiprocessing lock starts Python's resource tracker,
which is handed `sys.stderr.fileno()` to inherit. Inside the TUI, stderr is
Textual's print capture, whose `fileno()` answers -1 — and spawning a child
with -1 in its fd list raises. Nothing here uses more than one process, so
tqdm is kept to its thread lock and never builds the other one.

Run in a fresh interpreter: tqdm's lock is created once per process, so an
earlier test in this one would already have built it and hidden the bug.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent("""
    import sys

    class Capture:
        # What Textual installs as sys.stderr while the UI owns the screen.
        def write(self, text):
            return len(text)

        def flush(self):
            pass

        def fileno(self):
            return -1

    sys.stderr = Capture()
    sys.path.insert(0, sys.argv[1])
    try:
        from SpotiFLAC.core.progress import ProgressManager

        ProgressManager.initialize_master_bar(1, description="Progress")
        ProgressManager.clear_master_bar()
    except Exception as exc:
        sys.__stdout__.write(f"FAILED {type(exc).__name__}: {exc}\\n")
        raise SystemExit(1)
    sys.__stdout__.write("OK\\n")
    """)


def test_the_first_progress_bar_survives_a_stderr_without_a_real_fd() -> None:
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(REPO)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout.strip() == "OK", result.stdout + result.stderr

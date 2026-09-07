"""SpotiFLAC's terminal UI.

`--tui` is the guided mode: the wizard's fifteen questions as one screen you
can move around in, plus the live download queue the GUI has always had. It
is built on Textual, and it deliberately does **not** import `SpotiFLAC.app`
— that module imports pywebview at module level and its API object is
synchronous and thread-based. The TUI reads the same `api_mixins/*` and
`core/*` the GUI does, and hands the launcher the same `cfg` dict the wizard
does.

Importing this package must stay cheap and side-effect free: `launcher.py`
reaches for `SpotiFLACTui` only once `--tui` is on the command line, so
pulling Textual in at import time here would tax every other mode.
"""

from __future__ import annotations

__all__ = ["ConfigState", "SpotiFLACTui", "run_tui"]


def __getattr__(name: str):
    # Lazy on purpose: `from .tui import ConfigState` in a test must not drag
    # in Textual and an entire widget tree.
    if name == "ConfigState":
        from .config_state import ConfigState

        return ConfigState
    if name in {"SpotiFLACTui", "run_tui"}:
        from .app import SpotiFLACTui, run_tui

        return {"SpotiFLACTui": SpotiFLACTui, "run_tui": run_tui}[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)

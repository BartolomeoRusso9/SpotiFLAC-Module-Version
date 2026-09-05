"""`--tui`, and `--interactive` as its deprecated alias.

The wizard is gone: `--interactive` now opens the terminal UI and says so.
These tests drive the real branch in `launcher.amain()` rather than a copy of
the condition, so they still hold if the dispatch moves — and they are the
thing that would catch the alias quietly ceasing to warn, which is the whole
substance of a deprecation.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest

from SpotiFLAC import launcher


@pytest.fixture
def run_launcher(monkeypatch):
    """Runs amain() with the given argv, capturing whether the TUI opened.

    Everything amain() does before this branch — the banner, the update
    check, the extension bootstrap — is network and console noise here.
    """
    opened: list[bool] = []

    monkeypatch.setattr(launcher, "_print_welcome_banner", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_register_cli_registries", lambda *a, **k: None)
    monkeypatch.setattr(
        launcher,
        "_register_cli_registry_directories",
        lambda *a, **k: None,
    )

    async def _no_updates():
        return None

    monkeypatch.setattr(launcher, "check_for_updates_async", _no_updates)

    # Stubbed at the App, not at run_tui_async: the launcher runs inside
    # asyncio.run(), and the bug this guards against was `run_tui` opening a
    # *second* loop. Replacing run_tui_async would have skipped the very code
    # that raised, and the tests would have stayed green through it.
    import SpotiFLAC.tui.app as tui_app

    async def _fake_run_async(self):
        opened.append(True)

    monkeypatch.setattr(tui_app.SpotiFLACTui, "run_async", _fake_run_async)

    import SpotiFLAC.core.ffmpeg_check as ffmpeg_check
    import SpotiFLAC.core.node_check as node_check

    monkeypatch.setattr(ffmpeg_check, "print_ffmpeg_warning", lambda *a, **k: None)
    monkeypatch.setattr(node_check, "print_node_warning", lambda *a, **k: None)

    def _run(argv: list[str]) -> bool:
        monkeypatch.setattr("sys.argv", ["spotiflac", *argv])
        opened.clear()
        asyncio.run(launcher.amain())
        return bool(opened)

    return _run


def test_tui_opens_the_terminal_ui(run_launcher) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert run_launcher(["--tui"]) is True


def test_interactive_opens_the_terminal_ui_too(run_launcher) -> None:
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        assert run_launcher(["--interactive"]) is True


def test_interactive_warns_that_it_is_deprecated(run_launcher, capsys) -> None:
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        run_launcher(["--interactive"])

    messages = [str(w.message) for w in raised if w.category is DeprecationWarning]
    assert messages, "--interactive must warn before redirecting"
    assert "--tui" in messages[0]

    # A DeprecationWarning is hidden by default, and someone typing this at a
    # prompt is exactly who needs to be told, so it is also said out loud.
    assert "--interactive now opens the terminal UI" in capsys.readouterr().err


def test_the_tui_runs_on_the_launcher_own_loop(run_launcher) -> None:
    """The real launch bug: two nested asyncio.run() calls.

    `amain()` is already inside `asyncio.run()`. Textual's `App.run()` opens
    another one and dies with "asyncio.run() cannot be called from a running
    event loop" before drawing a frame — which is why the launcher awaits
    `run_async()` instead.
    """
    import asyncio
    import inspect

    from SpotiFLAC.tui.app import run_tui_async

    assert inspect.iscoroutinefunction(run_tui_async)
    assert run_launcher(["--tui"]) is True
    # And it left no loop of its own behind.
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


def test_tui_alone_does_not_warn(run_launcher) -> None:
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        run_launcher(["--tui"])

    assert not [w for w in raised if w.category is DeprecationWarning]


def test_the_wizard_is_gone() -> None:
    """`SpotiFLAC/interactive.py` was deleted, not merely bypassed.

    `--interactive` still parses and still warns — that is the deprecation —
    but there is no second guided frontend behind it any more, which was the
    whole point of the exercise.
    """
    import importlib.util

    assert importlib.util.find_spec("SpotiFLAC.interactive") is None

    import ast
    import inspect

    tree = ast.parse(inspect.getsource(launcher))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "run_interactive" not in imported

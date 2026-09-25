"""The signed-sessions panel, against a stubbed listing.

`list_signed_sessions` and friends are replaced: what is under test is that
the panel renders a row per session, clears the selected one and prunes —
not what happens to be in the ~/.spotiflac of the machine running the suite.
"""

from __future__ import annotations

import pytest

from tui_harness import app_of, drives_the_ui

from SpotiFLAC.core import signed_session_status as sss
from SpotiFLAC.tui.app import MODES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState

_SIGNED_INDEX = [key for key, _ in MODES].index("signed")


def _row(key, label, state, expires_in_s=3600.0):
    return {
        "key": key,
        "kind": "gateway",
        "label": label,
        "extensions": [label],
        "version": "1.0",
        "state": state,
        "expires_at": None,
        "expires_in_s": expires_in_s,
        "refresh_after": None,
        "refresh_in_s": None,
        "capabilities": [],
        "auth_paused_s": 0.0,
    }


@pytest.fixture
def stub_sessions(monkeypatch):
    rows = [
        _row("zarz-v2-a", "qobuz-web", sss.ACTIVE),
        _row("zarz-v2-b", "tidal-web", sss.EXPIRED, -60.0),
        _row("zarz-v2-c", "zarz-v2-c", sss.ORPHANED),
    ]
    calls = {"cleared": [], "pruned": 0}

    def _clear(key, directory=None):
        calls["cleared"].append(key)
        return True

    def _prune(directory=None, ext_dir=None):
        calls["pruned"] += 1
        rows[:] = [r for r in rows if r["state"] != sss.ORPHANED]
        return ["zarz-v2-c"]

    monkeypatch.setattr(sss, "list_signed_sessions", lambda *a, **k: list(rows))
    monkeypatch.setattr(sss, "clear_signed_session", _clear)
    monkeypatch.setattr(sss, "prune_orphaned_sessions", _prune)
    return calls


def _ready_state() -> ConfigState:
    return ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/spotiflac-test",
        services=["tidal"],
    )


async def _settled(pilot) -> None:
    for _ in range(12):
        await pilot.pause()


@drives_the_ui
async def test_lists_a_row_per_session(stub_sessions) -> None:
    from textual.widgets import DataTable

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        app_of(pilot).query_one("#sidebar").index = _SIGNED_INDEX
        await _settled(pilot)

        assert app_of(pilot).query_one("#signed-table", DataTable).row_count == 3
        status = str(app_of(pilot).query_one("#signed-status").render())
        assert "1 of 3 active" in status
        assert "1 orphaned" in status


@drives_the_ui
async def test_clear_selected_clears_the_cursor_row(stub_sessions) -> None:
    from textual.widgets import Button, DataTable

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        app_of(pilot).query_one("#sidebar").index = _SIGNED_INDEX
        await _settled(pilot)

        app_of(pilot).query_one("#signed-table", DataTable).move_cursor(row=1)
        app_of(pilot).query_one("#signed-clear", Button).press()
        await _settled(pilot)

        assert stub_sessions["cleared"] == ["zarz-v2-b"]


@drives_the_ui
async def test_prune_removes_orphans(stub_sessions) -> None:
    from textual.widgets import Button, DataTable

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        app_of(pilot).query_one("#sidebar").index = _SIGNED_INDEX
        await _settled(pilot)

        app_of(pilot).query_one("#signed-prune", Button).press()
        await _settled(pilot)

        assert stub_sessions["pruned"] == 1
        assert app_of(pilot).query_one("#signed-table", DataTable).row_count == 2

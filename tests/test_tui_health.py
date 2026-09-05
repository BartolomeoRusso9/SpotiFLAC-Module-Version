"""The health panel, without touching the network.

`run_health_check` is stubbed: what is under test is that the panel probes
only when asked, renders a row per provider, and survives the check failing —
not whether a lyrics server happens to be up while the suite runs.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from SpotiFLAC.core.health_check import HealthResult
from SpotiFLAC.tui.app import MODES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


_HEALTH_INDEX = [key for key, _ in MODES].index("health")

_RESULTS = [
    HealthResult("lrclib", "https://lrclib.net", "GET", True, 0.12, "200"),
    HealthResult("apple", "https://apple.example", "GET", True, 0.34, "200"),
    HealthResult("genius", "https://genius.example", "GET", False, 0.0, "timed out"),
]


@pytest.fixture
def stub_health(monkeypatch):
    """Replaces the probe, and counts how often it was called."""
    calls: list[int] = []

    async def _run(services=None, *, include_all_endpoints=True):
        calls.append(1)
        return list(_RESULTS)

    import SpotiFLAC.core.health_check as health_module

    monkeypatch.setattr(health_module, "run_health_check", _run)
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
async def test_opening_the_panel_does_not_probe(stub_health) -> None:
    """A panel that starts network traffic on sight is a panel you avoid."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _HEALTH_INDEX
        await _settled(pilot)

        assert stub_health == []
        assert "Not checked yet" in str(
            pilot.app.query_one("#health-status").render(),
        )


@drives_the_ui
async def test_checking_lists_a_row_per_provider(stub_health) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _HEALTH_INDEX
        await _settled(pilot)

        from textual.widgets import Button, DataTable

        pilot.app.query_one("#health-check", Button).press()
        await _settled(pilot)

        table = pilot.app.query_one("#health-table", DataTable)
        assert table.row_count == len(_RESULTS)

        rendered = str(pilot.app.query_one("#health-status").render())
        assert "2 of 3 reachable" in rendered
        assert "fall back" in rendered


@drives_the_ui
async def test_a_failing_check_is_reported_not_raised(monkeypatch) -> None:
    async def _explode(services=None, *, include_all_endpoints=True):
        msg = "no network"
        raise RuntimeError(msg)

    import SpotiFLAC.core.health_check as health_module

    monkeypatch.setattr(health_module, "run_health_check", _explode)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _HEALTH_INDEX
        await _settled(pilot)

        from textual.widgets import Button

        pilot.app.query_one("#health-check", Button).press()
        await _settled(pilot)

        assert "no network" in str(pilot.app.query_one("#health-status").render())


@drives_the_ui
async def test_all_reachable_says_so_plainly(monkeypatch) -> None:
    async def _all_up(services=None, *, include_all_endpoints=True):
        return [r for r in _RESULTS if r.ok]

    import SpotiFLAC.core.health_check as health_module

    monkeypatch.setattr(health_module, "run_health_check", _all_up)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _HEALTH_INDEX
        await _settled(pilot)

        from textual.widgets import Button

        pilot.app.query_one("#health-check", Button).press()
        await _settled(pilot)

        rendered = str(pilot.app.query_one("#health-status").render())
        assert "2 of 2 reachable" in rendered
        assert "fall back" not in rendered

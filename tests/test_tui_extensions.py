"""The extensions panel, with `registry_config` stubbed.

Registry links are real configuration on the user's machine — an environment
export, a `.env` file, a saved list — so nothing here touches the real ones.
What is checked is the panel's behaviour: it shows where each link came from,
it installs from the list after an edit rather than at the next launch, and a
failure is reported instead of raised.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from SpotiFLAC.tui.app import MODES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState
from SpotiFLAC.tui.extensions_view import ExtensionsPanel


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


_EXT_INDEX = [key for key, _ in MODES].index("extensions")


@pytest.fixture
def stub_registries(monkeypatch):
    """A registry list that lives in memory, plus a fake installer."""
    state = {
        "links": [
            {
                "url": "https://example.test/registry.json",
                "sources": ["custom"],
                "enabled": True,
            },
            {
                "url": "https://exported.test/registry.json",
                "sources": ["environment"],
                "enabled": True,
            },
        ],
        "installed": 0,
        "trust": [],
    }

    import SpotiFLAC.extensions.registry_config as registry_config

    monkeypatch.setattr(registry_config, "list_registries", lambda: list(state["links"]))
    monkeypatch.setattr(
        registry_config,
        "add_registry",
        lambda url: state["links"].append(
            {"url": url, "sources": ["custom"], "enabled": True},
        ),
    )

    def _remove(url):
        state["links"] = [link for link in state["links"] if link["url"] != url]

    monkeypatch.setattr(registry_config, "remove_registry", _remove)

    import SpotiFLAC.extensions.manager as manager_module

    class _Manager:
        def __init__(self, *args, **kwargs) -> None:
            state["installed"] += 1
            state["trust"].append(kwargs.get("min_trust_tier"))

    monkeypatch.setattr(manager_module, "ExtensionManager", _Manager)
    return state


def _ready_state() -> ConfigState:
    return ConfigState(output_dir="/tmp/spotiflac-test", services=["tidal"])


async def _settled(pilot) -> None:
    for _ in range(15):
        await pilot.pause()


@drives_the_ui
async def test_links_are_listed_with_where_they_came_from(stub_registries) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _EXT_INDEX
        await _settled(pilot)

        from textual.widgets import DataTable

        table = pilot.app.query_one("#registry-table", DataTable)
        assert table.row_count == 2

        # A link exported in the terminal cannot be edited from here, and
        # saying where it came from is more useful than pretending it can.
        sources = [str(table.get_row_at(row)[1]) for row in range(table.row_count)]
        assert "added here" in sources
        assert "terminal export" in sources


@drives_the_ui
async def test_adding_a_link_installs_from_it_immediately(stub_registries) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _EXT_INDEX
        await _settled(pilot)

        from textual.widgets import Button, DataTable, Input

        pilot.app.query_one("#registry-url", Input).value = "https://new.test/r.json"
        pilot.app.query_one("#registry-add", Button).press()
        await _settled(pilot)

        assert any(
            link["url"] == "https://new.test/r.json" for link in stub_registries["links"]
        )
        assert stub_registries["installed"] == 1
        assert pilot.app.query_one("#registry-table", DataTable).row_count == 3
        # The field is cleared, so the next paste does not append to it.
        assert pilot.app.query_one("#registry-url", Input).value == ""


@drives_the_ui
async def test_the_trust_floor_from_the_command_line_is_honoured(
    stub_registries,
) -> None:
    """`--min-trust-tier` has to reach an install started from this panel."""
    async with SpotiFLACTui(_ready_state(), "signed").run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _EXT_INDEX
        await _settled(pilot)

        from textual.widgets import Button, Input

        pilot.app.query_one("#registry-url", Input).value = "https://new.test/r.json"
        pilot.app.query_one("#registry-add", Button).press()
        await _settled(pilot)

        assert stub_registries["trust"] == ["signed"]


@drives_the_ui
async def test_removing_the_selected_link(stub_registries) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _EXT_INDEX
        await _settled(pilot)

        from textual.widgets import Button, DataTable

        table = pilot.app.query_one("#registry-table", DataTable)
        table.move_cursor(row=0)
        pilot.app.query_one("#registry-remove", Button).press()
        await _settled(pilot)

        assert [link["url"] for link in stub_registries["links"]] == [
            "https://exported.test/registry.json",
        ]
        assert table.row_count == 1


@drives_the_ui
async def test_adding_nothing_says_so(stub_registries) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _EXT_INDEX
        await _settled(pilot)

        from textual.widgets import Button

        pilot.app.query_one("#registry-add", Button).press()
        await _settled(pilot)

        assert "Paste a registry link" in str(
            pilot.app.query_one("#registry-status").render(),
        )
        assert stub_registries["installed"] == 0


@drives_the_ui
async def test_a_failing_add_is_reported_not_raised(monkeypatch) -> None:
    import SpotiFLAC.extensions.registry_config as registry_config

    monkeypatch.setattr(registry_config, "list_registries", list)

    def _explode(url):
        msg = "that is not a registry"
        raise RuntimeError(msg)

    monkeypatch.setattr(registry_config, "add_registry", _explode)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _EXT_INDEX
        await _settled(pilot)

        from textual.widgets import Button, Input

        pilot.app.query_one("#registry-url", Input).value = "nonsense"
        pilot.app.query_one("#registry-add", Button).press()
        await _settled(pilot)

        assert "that is not a registry" in str(
            pilot.app.query_one("#registry-status").render(),
        )


@drives_the_ui
async def test_an_empty_list_says_so(monkeypatch) -> None:
    import SpotiFLAC.extensions.registry_config as registry_config

    monkeypatch.setattr(registry_config, "list_registries", list)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _EXT_INDEX
        await _settled(pilot)

        assert "No registry links configured" in str(
            pilot.app.query_one("#registry-status").render(),
        )

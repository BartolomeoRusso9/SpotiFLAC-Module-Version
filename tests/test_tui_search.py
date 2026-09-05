"""The search panel, with the catalogue stubbed.

What matters here is what happens to a result once it is picked: it becomes
the Download panel's URL and nothing else. A search panel that also started
downloads would be a second copy of the Download panel's rules, and the two
would drift.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from SpotiFLAC.tui.app import MODES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState
from SpotiFLAC.tui.search_view import SearchPanel


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


_SEARCH_INDEX = [key for key, _ in MODES].index("search")

_RESULTS = {
    "tracks": [
        {
            "id": "t1",
            "name": "So What",
            "artists": "Miles Davis",
            "album": "Kind of Blue",
            "external_url": "https://open.spotify.com/track/t1",
        },
        {
            "id": "t2",
            "name": "No Link Here",
            "artists": "Nobody",
            "external_url": "",
        },
    ],
    "albums": [
        {
            "id": "a1",
            "name": "Kind of Blue",
            "artists": "Miles Davis",
            "external_url": "https://open.spotify.com/album/a1",
        },
    ],
    "artists": [],
    "playlists": [],
}


@pytest.fixture
def stub_search(monkeypatch):
    queries: list[str] = []

    async def _search(query, limit=50):
        queries.append(query)
        return _RESULTS

    import SpotiFLAC.api_mixins.search as search_module

    monkeypatch.setattr(search_module, "search_metadata_async", _search)
    return queries


def _ready_state() -> ConfigState:
    return ConfigState(output_dir="/tmp/spotiflac-test", services=["tidal"])


async def _settled(pilot) -> None:
    for _ in range(12):
        await pilot.pause()


@drives_the_ui
async def test_slash_goes_to_the_search_box(stub_search) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").focus()
        await pilot.pause()

        await pilot.press("slash")
        await _settled(pilot)

        assert pilot.app.query_one("#panels").current == "search"
        assert pilot.app.focused is pilot.app.query_one("#search-query")


@drives_the_ui
async def test_results_are_listed_and_linkless_ones_dropped(stub_search) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SEARCH_INDEX
        await _settled(pilot)

        panel = pilot.app.query_one("#search", SearchPanel)
        panel.search("miles")
        await _settled(pilot)

        from textual.widgets import DataTable

        table = pilot.app.query_one("#search-results", DataTable)
        # Two of the three have a link; the third would be a decorative row.
        assert table.row_count == 2
        assert stub_search == ["miles"]
        assert "2 result(s)" in str(pilot.app.query_one("#search-status").render())


@drives_the_ui
async def test_picking_a_result_fills_the_download_url(stub_search) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SEARCH_INDEX
        await _settled(pilot)

        panel = pilot.app.query_one("#search", SearchPanel)
        panel.search("miles")
        await _settled(pilot)

        panel.post_message(
            SearchPanel.UrlChosen("https://open.spotify.com/album/a1", "Kind of Blue"),
        )
        await _settled(pilot)

        from textual.widgets import Input

        assert (
            pilot.app.query_one("#cfg-url", Input).value
            == "https://open.spotify.com/album/a1"
        )
        assert pilot.app.state.url == "https://open.spotify.com/album/a1"
        assert pilot.app.query_one("#panels").current == "download"
        assert "Kind of Blue" in str(pilot.app.query_one("#status").content)


@drives_the_ui
async def test_an_empty_query_is_not_searched(stub_search) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SEARCH_INDEX
        await _settled(pilot)

        pilot.app.query_one("#search", SearchPanel).search("   ")
        await _settled(pilot)

        assert stub_search == []
        assert "Type something" in str(pilot.app.query_one("#search-status").render())


@drives_the_ui
async def test_a_failing_search_is_reported_not_raised(monkeypatch) -> None:
    async def _explode(query, limit=50):
        msg = "spotify said no"
        raise RuntimeError(msg)

    import SpotiFLAC.api_mixins.search as search_module

    monkeypatch.setattr(search_module, "search_metadata_async", _explode)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SEARCH_INDEX
        await _settled(pilot)

        pilot.app.query_one("#search", SearchPanel).search("miles")
        await _settled(pilot)

        assert "spotify said no" in str(
            pilot.app.query_one("#search-status").render(),
        )


@drives_the_ui
async def test_nothing_found_says_so(monkeypatch) -> None:
    async def _nothing(query, limit=50):
        return {"tracks": [], "albums": [], "artists": [], "playlists": []}

    import SpotiFLAC.api_mixins.search as search_module

    monkeypatch.setattr(search_module, "search_metadata_async", _nothing)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SEARCH_INDEX
        await _settled(pilot)

        pilot.app.query_one("#search", SearchPanel).search("zzzz")
        await _settled(pilot)

        assert "Nothing for" in str(pilot.app.query_one("#search-status").render())

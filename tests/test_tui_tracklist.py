"""Picking tracks in the TUI, and the run honouring the pick.

The panel itself is a table with a tick column; the part worth guarding is
the seam behind it. A selection that looks right on screen and does not reach
`_run_download_async` is the failure nobody would notice until the wrong
files landed on disk, so the last test here follows the pick all the way into
the `cfg` the launcher is handed.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

import SpotiFLAC.downloader as downloader
import SpotiFLAC.launcher as launcher_module
from SpotiFLAC.tui.app import MODES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState
from SpotiFLAC.tui.tracklist_view import TracklistPanel


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


_TRACKS_INDEX = [key for key, _ in MODES].index("tracks")
_ALBUM = "https://open.spotify.com/album/a1"


class _Track:
    def __init__(self, title, track_id=None):
        self.title = title
        self.artists = "Miles Davis"
        self.album = "Kind of Blue"
        if track_id is not None:
            self.id = track_id


@pytest.fixture
def stub_album(monkeypatch):
    """Five tracks, the fourth of them without a link of its own."""
    tracks = [
        _Track("So What", "1"),
        _Track("Freddie Freeloader", "2"),
        _Track("Blue in Green", "3"),
        _Track("All Blues"),
        _Track("Flamenco Sketches", "5"),
    ]

    async def _get(client, url, **kwargs):
        return ("Kind of Blue", tracks, "cover.jpg", {})

    monkeypatch.setattr(downloader, "_call_metadata_get_url", _get)
    return tracks


@pytest.fixture
def capture_runs(monkeypatch):
    """Collects the cfg a run would have been given, instead of running it.

    Through monkeypatch, not a bare assignment: `run_download_from_cfg` is a
    module attribute, and replacing it by hand would leave every later test
    in the session downloading into a list.
    """
    seen: list[dict] = []

    async def _capture(cfg, log_level):
        seen.append(cfg)

    monkeypatch.setattr(launcher_module, "run_download_from_cfg", _capture)
    return seen


def _ready_state() -> ConfigState:
    return ConfigState(url=_ALBUM, output_dir="/tmp/spotiflac-test", services=["tidal"])


async def _settled(pilot) -> None:
    for _ in range(15):
        await pilot.pause()


async def _loaded(pilot) -> TracklistPanel:
    pilot.app.query_one("#sidebar").index = _TRACKS_INDEX
    await _settled(pilot)
    panel = pilot.app.query_one("#tracks", TracklistPanel)
    panel.load()
    await _settled(pilot)
    return panel


# ---------------------------------------------------------------------------


@drives_the_ui
async def test_nothing_is_fetched_until_you_ask(stub_album) -> None:
    """Opening the panel must not fire a request at a URL you are editing."""
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        pilot.app.query_one("#sidebar").index = _TRACKS_INDEX
        await _settled(pilot)

        panel = pilot.app.query_one("#tracks", TracklistPanel)
        assert len(panel.tracklist) == 0
        assert "Press Load tracks" in str(pilot.app.query_one("#tracks-status").render())


@drives_the_ui
async def test_loading_lists_the_tracks_all_selected(stub_album) -> None:
    """The panel exists to take tracks away, so it opens with all of them."""
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        panel = await _loaded(pilot)

        from textual.widgets import DataTable

        table = pilot.app.query_one("#tracks-table", DataTable)
        assert table.row_count == 5
        assert panel.selected_indices() == [0, 1, 2, 3, 4]
        assert panel.is_whole_collection

        status = str(pilot.app.query_one("#tracks-status").render())
        assert "5 track(s) in Kind of Blue" in status


@drives_the_ui
async def test_a_track_with_no_link_of_its_own_is_marked(stub_album) -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        await _loaded(pilot)

        from textual.widgets import DataTable

        table = pilot.app.query_one("#tracks-table", DataTable)
        assert str(table.get_row_at(3)[4]) == "no link"
        assert str(table.get_row_at(0)[4]) == "Kind of Blue"


@drives_the_ui
async def test_space_toggles_the_row_under_the_cursor(stub_album) -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        panel = await _loaded(pilot)

        from textual.widgets import DataTable

        table = pilot.app.query_one("#tracks-table", DataTable)
        table.move_cursor(row=1)
        panel.action_toggle_row()
        await pilot.pause()

        assert panel.selected_indices() == [0, 2, 3, 4]
        assert not panel.is_whole_collection
        assert str(table.get_row_at(1)[0]).strip() in {"·", ""}
        assert str(table.get_row_at(0)[0]) == "✔"

        panel.action_toggle_row()
        await pilot.pause()
        assert panel.selected_indices() == [0, 1, 2, 3, 4]


@drives_the_ui
async def test_all_none_and_invert(stub_album) -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        panel = await _loaded(pilot)

        panel.action_select_none()
        await pilot.pause()
        assert panel.selected_indices() == []
        assert "nothing would download" in str(
            pilot.app.query_one("#tracks-status").render(),
        )

        panel.action_invert()
        await pilot.pause()
        assert panel.selected_indices() == [0, 1, 2, 3, 4]

        panel.action_select_none()
        panel.action_select_all()
        await pilot.pause()
        assert panel.selected_indices() == [0, 1, 2, 3, 4]


@drives_the_ui
async def test_a_bad_link_is_reported_not_raised(monkeypatch) -> None:
    async def _boom(client, url, **kwargs):
        msg = "no such album"
        raise RuntimeError(msg)

    monkeypatch.setattr(downloader, "_call_metadata_get_url", _boom)

    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        panel = await _loaded(pilot)

        assert len(panel.tracklist) == 0
        assert "no such album" in str(pilot.app.query_one("#tracks-status").render())


@drives_the_ui
async def test_loading_without_a_url_says_where_to_set_one() -> None:
    state = ConfigState(output_dir="/tmp/o", services=["tidal"])
    async with SpotiFLACTui(state).run_test(size=(110, 40)) as pilot:
        await _loaded(pilot)
        assert "Set a URL on the Download panel" in str(
            pilot.app.query_one("#tracks-status").render(),
        )


# ---------------------------------------------------------------------------
# The seam: does the pick reach the download?
# ---------------------------------------------------------------------------


@drives_the_ui
async def test_the_whole_album_is_fetched_as_the_album(stub_album, capture_runs) -> None:
    """All selected keeps the collection URL, ordering and numbering intact."""
    seen = capture_runs

    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        await _loaded(pilot)
        pilot.app.action_start_download()
        for _ in range(60):
            await pilot.pause()
            if seen:
                break

    assert seen and seen[0]["url"] == _ALBUM


@drives_the_ui
async def test_a_pick_reaches_the_downloader_as_track_links(stub_album, capture_runs) -> None:
    """The failure this guards: a pick that looks right and downloads the lot."""
    seen = capture_runs

    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        panel = await _loaded(pilot)
        panel.action_select_none()
        from textual.widgets import DataTable

        table = pilot.app.query_one("#tracks-table", DataTable)
        for row in (0, 2):
            table.move_cursor(row=row)
            panel.action_toggle_row()
        await pilot.pause()

        pilot.app.action_start_download()
        for _ in range(60):
            await pilot.pause()
            if seen:
                break

    assert seen
    assert seen[0]["url"] == [
        "https://open.spotify.com/track/1",
        "https://open.spotify.com/track/3",
    ]


@drives_the_ui
async def test_starting_with_nothing_picked_is_refused(stub_album, capture_runs) -> None:
    """An empty pick used to mean 'no opinion', which downloaded everything."""
    seen = capture_runs

    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        panel = await _loaded(pilot)
        panel.action_select_none()
        await pilot.pause()

        pilot.app.action_start_download()
        await _settled(pilot)

        assert seen == []
        assert "Nothing selected" in str(pilot.app.query_one("#status").content)
        assert pilot.app.query_one("#panels").current == "tracks"


@drives_the_ui
async def test_an_unloaded_panel_leaves_the_url_alone(stub_album, capture_runs) -> None:
    """No pick is not the same as an empty pick."""
    seen = capture_runs

    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        pilot.app.action_start_download()
        for _ in range(60):
            await pilot.pause()
            if seen:
                break

    assert seen and seen[0]["url"] == _ALBUM


@drives_the_ui
async def test_the_command_panel_admits_the_cli_cannot_do_this(stub_album) -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        panel = await _loaded(pilot)
        panel.action_select_none()
        from textual.widgets import DataTable

        pilot.app.query_one("#tracks-table", DataTable).move_cursor(row=0)
        panel.action_toggle_row()
        await _settled(pilot)

        rendered = str(pilot.app.query_one("#command").content)
        assert "1 of 5 tracks are picked" in rendered
        assert "fetches the whole link" in rendered

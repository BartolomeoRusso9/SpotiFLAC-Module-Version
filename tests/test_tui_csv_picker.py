"""Picking a track list from the TUI.

The scan and the path handling are `core.csv_picker`'s, and have their own
tests; what is checked here is the screen's judgement — that it reads a file
before accepting it, that it refuses one it cannot use, and that accepting
one fills the form field rather than only the state.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from SpotiFLAC.tui.app import SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState
from SpotiFLAC.tui.csv_picker_screen import CsvPickerScreen


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def _ready_state() -> ConfigState:
    return ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/spotiflac-test",
        services=["tidal"],
    )


async def _settled(pilot) -> None:
    for _ in range(12):
        await pilot.pause()


@pytest.fixture
def track_list(tmp_path):
    path = tmp_path / "wishlist.csv"
    path.write_text(
        "title,artist\n"
        "Blue in Green,Miles Davis\n"
        "So What,Miles Davis\n"
        "Flamenco Sketches,Miles Davis\n"
        "All Blues,Miles Davis\n",
    )
    return path


@drives_the_ui
async def test_browsing_opens_the_picker() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Button

        pilot.app.query_one("#csv-browse", Button).press()
        await _settled(pilot)

        assert isinstance(pilot.app.screen, CsvPickerScreen)

        await pilot.press("escape")
        await _settled(pilot)
        assert not isinstance(pilot.app.screen, CsvPickerScreen)


@drives_the_ui
async def test_accepting_a_file_fills_the_form(track_list) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Button, Input

        pilot.app.query_one("#csv-browse", Button).press()
        await _settled(pilot)

        screen = pilot.app.screen
        screen.query_one("#csv-path", Input).value = str(track_list)
        screen.query_one("#csv-accept", Button).press()
        await _settled(pilot)

        assert not isinstance(pilot.app.screen, CsvPickerScreen)
        assert pilot.app.query_one("#cfg-csv_path", Input).value == str(track_list)
        assert pilot.app.state.csv_path == str(track_list)
        # A CSV replaces the URL, and the state says so.
        assert pilot.app.state.to_cfg()["url"] == ""


@drives_the_ui
async def test_the_preview_says_what_is_in_the_file(track_list) -> None:
    """A file that parses is not necessarily the right file."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Button, Input

        pilot.app.query_one("#csv-browse", Button).press()
        await _settled(pilot)

        screen = pilot.app.screen
        screen.query_one("#csv-path", Input).value = str(track_list)
        preview = await screen._preview(str(track_list))
        await _settled(pilot)

        assert preview == str(track_list)
        shown = str(screen.query_one("#csv-preview").content)
        assert "4 tracks" in shown
        assert "Blue in Green" in shown


@drives_the_ui
async def test_a_missing_file_is_refused(tmp_path) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Button, Input

        pilot.app.query_one("#csv-browse", Button).press()
        await _settled(pilot)

        screen = pilot.app.screen
        missing = str(tmp_path / "nope.csv")
        screen.query_one("#csv-path", Input).value = missing
        screen.query_one("#csv-accept", Button).press()
        await _settled(pilot)

        # Still open: a refusal has to leave you somewhere you can fix it.
        assert isinstance(pilot.app.screen, CsvPickerScreen)
        assert "No file at" in str(screen.query_one("#csv-preview").content)


@drives_the_ui
async def test_a_track_list_with_no_tracks_is_refused(tmp_path) -> None:
    """A header and nothing else parses as a file but not as a track list.

    `csv_source` already refuses it, and says what it was looking for; the
    picker's job is to show that instead of accepting the file and failing
    later.
    """
    empty = tmp_path / "empty.csv"
    empty.write_text("title,artist\n")

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Button, Input

        pilot.app.query_one("#csv-browse", Button).press()
        await _settled(pilot)

        screen = pilot.app.screen
        screen.query_one("#csv-path", Input).value = str(empty)
        screen.query_one("#csv-accept", Button).press()
        await _settled(pilot)

        assert isinstance(pilot.app.screen, CsvPickerScreen)
        shown = str(screen.query_one("#csv-preview").content)
        assert "no track found" in shown
        assert pilot.app.state.csv_path == ""


@drives_the_ui
async def test_a_quoted_path_is_understood(track_list) -> None:
    """Dragging a path into a shell wraps it in quotes; so do people."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Button, Input

        pilot.app.query_one("#csv-browse", Button).press()
        await _settled(pilot)

        screen = pilot.app.screen
        screen.query_one("#csv-path", Input).value = f'"{track_list}"'
        screen.query_one("#csv-accept", Button).press()
        await _settled(pilot)

        assert pilot.app.state.csv_path == str(track_list)

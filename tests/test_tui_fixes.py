"""Four things that were wrong, pinned so they stay fixed.

Each of these is a defect that a passing test suite happily allowed, which
is the only reason they are gathered here rather than scattered: they are
what the UI got wrong in use, and the tests exist because using it is how
they were found.
"""

from __future__ import annotations

import asyncio
import functools

from SpotiFLAC.core.paths import default_download_dir
from SpotiFLAC.tui.app import SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState
from SpotiFLAC.tui.config_view import quality_choices


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def _ready_state() -> ConfigState:
    return ConfigState(url="https://open.spotify.com/track/x", services=["tidal"])


async def _settled(pilot) -> None:
    for _ in range(6):
        await pilot.pause()


# ---------------------------------------------------------------------------
# 1 · The log pane closes again
# ---------------------------------------------------------------------------


@drives_the_ui
async def test_closing_the_log_never_leaves_the_focus_nowhere() -> None:
    """The actual defect: Textual drops focus when its widget is hidden.

    Close the log while reading it and nothing was focused at all — no
    cursor, arrow keys dead, an interface that looks broken and cannot be
    steered back.
    """
    async with SpotiFLACTui(_ready_state()).run_test(size=(104, 38)) as pilot:
        await _settled(pilot)
        pane = pilot.app.query_one("#log-pane")

        pilot.app._set_log_visible(True)
        pilot.app.query_one("#log").focus()
        await pilot.pause()
        assert type(pilot.app.focused).__name__ == "RichLog"

        await pilot.press("ctrl+l")
        await pilot.pause()

        assert pane.display is False
        assert pilot.app.focused is not None, "the focus went nowhere"
        assert pilot.app.focused.id == "sidebar"


@drives_the_ui
async def test_the_log_toggles_from_wherever_the_focus_is() -> None:
    """Ctrl+L is priority-bound, so nothing focused can swallow it."""
    async with SpotiFLACTui(_ready_state()).run_test(size=(104, 38)) as pilot:
        await _settled(pilot)
        pane = pilot.app.query_one("#log-pane")

        for widget_id in ("#cfg-url", "#cfg-output_dir", "#sidebar"):
            pilot.app.query_one(widget_id).focus()
            await pilot.pause()

            await pilot.press("ctrl+l")
            await pilot.pause()
            assert pane.display is True, f"would not open from {widget_id}"

            await pilot.press("ctrl+l")
            await pilot.pause()
            assert pane.display is False, f"would not close from {widget_id}"


@drives_the_ui
async def test_escape_closes_the_log() -> None:
    """A second way out, for terminals that keep Ctrl+L for themselves."""
    async with SpotiFLACTui(_ready_state()).run_test(size=(104, 38)) as pilot:
        await _settled(pilot)
        pane = pilot.app.query_one("#log-pane")

        pilot.app._set_log_visible(True)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pane.display is False

        # Esc on an already-closed log does nothing rather than reopening it.
        await pilot.press("escape")
        await pilot.pause()
        assert pane.display is False


@drives_the_ui
async def test_a_run_opens_the_log_through_the_same_door() -> None:
    """So the focus rule applies to the run's own opening too."""
    async with SpotiFLACTui(_ready_state()).run_test(size=(104, 38)) as pilot:
        await _settled(pilot)
        pilot.app.action_start_download()
        await _settled(pilot)

        assert pilot.app.query_one("#log-pane").display is True
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert pilot.app.query_one("#log-pane").display is False


# ---------------------------------------------------------------------------
# 2 · Only the three tiers worth choosing
# ---------------------------------------------------------------------------


def test_the_quality_menu_offers_three_tiers_at_most() -> None:
    with_tidal = [value for _label, value in quality_choices(["tidal"])]
    assert with_tidal == ["HI_RES_LOSSLESS", "LOSSLESS", "DOLBY_ATMOS"]

    without = [value for _label, value in quality_choices(["deezer", "qobuz"])]
    assert without == ["HI_RES_LOSSLESS", "LOSSLESS"]

    assert "HI_RES" not in without
    assert "HIGH" not in without
    assert "LOW" not in without


@drives_the_ui
async def test_the_menu_follows_the_providers() -> None:
    """Atmos appears when Tidal is picked and goes when it is dropped."""
    from textual.widgets import Select, SelectionList

    state = ConfigState(url="https://x/y", services=["deezer"])
    async with SpotiFLACTui(state).run_test(size=(104, 40)) as pilot:
        await _settled(pilot)
        select = pilot.app.query_one("#cfg-quality", Select)

        def offered() -> list[str]:
            return [
                str(value)
                for _label, value in select._options
                if value is not Select.BLANK
            ]

        assert "DOLBY_ATMOS" not in offered()

        providers = pilot.app.query_one("#cfg-services", SelectionList)
        if "tidal" in [str(option.value) for option in providers.options]:
            providers.select(providers.get_option_at_index(
                [str(o.value) for o in providers.options].index("tidal"),
            ))
            await _settled(pilot)
            assert "DOLBY_ATMOS" in offered()


@drives_the_ui
async def test_dropping_tidal_takes_atmos_with_it() -> None:
    """The chosen value has to move too, not just the menu."""
    state = ConfigState(
        url="https://x/y",
        services=["tidal"],
        quality="DOLBY_ATMOS",
    )
    async with SpotiFLACTui(state).run_test(size=(104, 40)) as pilot:
        await _settled(pilot)
        assert pilot.app.state.to_cfg()["quality"] == "DOLBY_ATMOS"

        pilot.app.state.services = ["deezer"]
        pilot.app.query_one("#download")._refresh_dependencies()
        await _settled(pilot)

        assert pilot.app.state.to_cfg()["quality"] == "HI_RES_LOSSLESS"


# ---------------------------------------------------------------------------
# 3 & 4 · Where downloads land
# ---------------------------------------------------------------------------


@drives_the_ui
async def test_the_folder_field_opens_on_music_spotiflac() -> None:
    from textual.widgets import Input

    async with SpotiFLACTui().run_test(size=(104, 40)) as pilot:
        await _settled(pilot)

        assert pilot.app.state.output_dir == default_download_dir()
        assert (
            pilot.app.query_one("#cfg-output_dir", Input).value
            == default_download_dir()
        )


@drives_the_ui
async def test_last_run_folder_no_longer_overrides_the_default(monkeypatch) -> None:
    """A one-off download elsewhere must not become where everything goes."""
    import SpotiFLAC.core.session_memory as session_memory

    async def _elsewhere() -> str:
        return "/tmp/somewhere-i-downloaded-once"

    monkeypatch.setattr(session_memory, "get_last_folder_async", _elsewhere)

    async with SpotiFLACTui().run_test(size=(104, 40)) as pilot:
        for _ in range(15):
            await pilot.pause()
        assert pilot.app.state.output_dir == default_download_dir()


def test_the_gui_and_the_tui_default_to_the_same_folder() -> None:
    """Two frontends with two defaults is two libraries on one machine."""
    import pytest

    pytest.importorskip("webview")
    from SpotiFLAC.app import DEFAULT_DOWNLOAD_DIR
    from SpotiFLAC.tui.app import DEFAULT_OUTPUT_DIR

    assert DEFAULT_DOWNLOAD_DIR == DEFAULT_OUTPUT_DIR == default_download_dir()

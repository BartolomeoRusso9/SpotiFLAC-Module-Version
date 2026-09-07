"""Choosing a saved profile from the Configuration panel.

Profiles were reachable only from the Session panel, two panels away from
the settings they replace. This is the same store offered where the
settings are, as a menu under Destination.

The picker is not a `cfg-` widget: every one of those writes a single field
of ConfigState, and this one replaces the state wholesale, so it announces
:class:`ConfigPanel.ProfileChosen` and the app rebuilds the form.
"""

from __future__ import annotations


import pytest
from textual.widgets import Input, Select, Static

from tui_harness import drives_the_ui

from SpotiFLAC.tui.app import MODES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState

_DOWNLOAD_INDEX = [key for key, _ in MODES].index("download")

_SAVED_PROFILE = {
    "url": "https://open.spotify.com/album/from-profile",
    "output_dir": "/tmp/from-profile",
    "services": ["qobuz"],
    "quality": "HI_RES",
    "use_artist_subfolders": True,
    "track_max_retries": 4,
}


@pytest.fixture
def stub_profiles(monkeypatch):
    import SpotiFLAC.core.profiles as profiles_module

    saved: dict[str, dict] = {
        "weekend": dict(_SAVED_PROFILE),
        "archive": dict(_SAVED_PROFILE, output_dir="/tmp/archive"),
    }

    async def _list():
        return sorted(saved)

    async def _get(name):
        return saved.get(name)

    monkeypatch.setattr(profiles_module, "list_profiles_async", _list)
    monkeypatch.setattr(profiles_module, "get_profile_async", _get)
    return saved


@pytest.fixture
def stub_no_profiles(monkeypatch):
    import SpotiFLAC.core.profiles as profiles_module

    async def _list():
        return []

    monkeypatch.setattr(profiles_module, "list_profiles_async", _list)


def _ready_state() -> ConfigState:
    return ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/spotiflac-test",
        services=["tidal"],
    )


async def _settled(pilot) -> None:
    """Waits for the panel's background loads and remounts to land."""
    for _ in range(12):
        await pilot.pause()


def _picker(pilot) -> Select:
    return pilot.app.query_one("#profile-picker", Select)


def _offered_profiles(pilot) -> list[str]:
    """The names on the picker's menu, as the widget renders them.

    For profiles the label *is* the value, so the rendered prompts answer the
    question without reaching for the option values.
    """
    from textual.widgets import OptionList

    picker = _picker(pilot)
    menu = picker.query_one(OptionList)
    rows = [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]
    # The blank row at the top is the picker's own prompt, not a profile.
    return [row for row in rows if row != str(picker.prompt)]


# --- the menu -------------------------------------------------------------


@drives_the_ui
async def test_the_saved_profiles_are_offered(stub_profiles) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)
        # Read off the menu the widget actually shows, rather than out of
        # Select._options — a private attribute of somebody else's widget,
        # and one that has already changed shape between Textual releases.
        assert _offered_profiles(pilot) == ["archive", "weekend"]


@drives_the_ui
async def test_it_sits_in_the_configuration_panel(stub_profiles) -> None:
    """Under Destination, not in a panel of its own."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)
        panel = pilot.app.query_one("#download")
        assert _picker(pilot) in panel.query(Select)


@drives_the_ui
async def test_with_nothing_saved_it_says_where_to_save_one(stub_no_profiles) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)
        said = str(pilot.app.query_one("#profile-status", Static).content)
        assert "No profiles saved yet" in said


# --- picking one ----------------------------------------------------------


@drives_the_ui
async def test_picking_one_replaces_every_setting(stub_profiles) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)

        _picker(pilot).value = "weekend"
        await _settled(pilot)

        state = pilot.app.state
        assert state.profile_loaded == "weekend"
        assert state.output_dir == "/tmp/from-profile"
        assert state.services == ["qobuz"]
        assert state.track_max_retries == 4


@drives_the_ui
async def test_the_form_is_rebuilt_not_just_the_state(stub_profiles) -> None:
    """A control still showing the old profile is the failure this guards."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)

        _picker(pilot).value = "archive"
        await _settled(pilot)

        shown = pilot.app.query_one("#cfg-output_dir", Input).value
        assert shown == "/tmp/archive"


@drives_the_ui
async def test_the_rebuilt_menu_shows_which_profile_is_loaded(stub_profiles) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)

        _picker(pilot).value = "weekend"
        await _settled(pilot)

        assert _picker(pilot).value == "weekend"
        said = str(pilot.app.query_one("#profile-status", Static).content)
        assert "weekend" in said


@drives_the_ui
async def test_settling_on_the_loaded_profile_does_not_reload_it(
    stub_profiles,
) -> None:
    """The regression that made the panel rebuild itself without end.

    Assigning the picker's value raises Changed exactly as a click does, and
    Changed arrives through the message queue — so a flag held around the
    assignment is already down when the handler runs. Each spurious load
    rebuilt the panel, which set the value again, which loaded again.
    """
    loads: list[str] = []
    import SpotiFLAC.core.profiles as profiles_module

    real_get = profiles_module.get_profile_async

    async def _counting_get(name):
        loads.append(name)
        return await real_get(name)

    profiles_module.get_profile_async = _counting_get
    try:
        async with SpotiFLACTui(_ready_state()).run_test() as pilot:
            await _settled(pilot)
            _picker(pilot).value = "weekend"
            await _settled(pilot)
            await _settled(pilot)
    finally:
        profiles_module.get_profile_async = real_get

    assert loads == ["weekend"]


@drives_the_ui
async def test_switching_between_two_profiles_works(stub_profiles) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)

        _picker(pilot).value = "weekend"
        await _settled(pilot)
        assert pilot.app.state.output_dir == "/tmp/from-profile"

        _picker(pilot).value = "archive"
        await _settled(pilot)
        assert pilot.app.state.output_dir == "/tmp/archive"
        assert pilot.app.state.profile_loaded == "archive"


@drives_the_ui
async def test_an_unreadable_store_leaves_the_form_alone(monkeypatch) -> None:
    """The panel is the settings screen; profiles are a convenience on it."""
    import SpotiFLAC.core.profiles as profiles_module

    async def _boom():
        raise OSError("profiles.json is unreadable")

    monkeypatch.setattr(profiles_module, "list_profiles_async", _boom)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        await _settled(pilot)
        assert pilot.app.query_one("#cfg-output_dir", Input).value == (
            "/tmp/spotiflac-test"
        )
        said = str(pilot.app.query_one("#profile-status", Static).content)
        assert "unreadable" in said

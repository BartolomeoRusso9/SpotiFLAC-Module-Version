"""History and profiles, the two wizard sections people came back for.

Both talk to `core/history` and `core/profiles`, which read real files, so
the tests stub those modules' functions rather than writing to the user's
own history. What is under test is the panel's behaviour — picking a URL
fills the form, loading a profile replaces every setting — not the storage,
which has its own tests.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from SpotiFLAC.tui.app import MODES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState
from SpotiFLAC.tui.session_view import SessionPanel


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


_SESSION_INDEX = [key for key, _ in MODES].index("session")

_SAVED_PROFILE = {
    "url": "https://open.spotify.com/album/from-profile",
    "output_dir": "/tmp/from-profile",
    "services": ["qobuz"],
    "quality": "HI_RES",
    "use_artist_subfolders": True,
    "track_max_retries": 4,
    "_profile_loaded": "ignored, from_cfg reads the name separately",
}


@pytest.fixture
def stub_session(monkeypatch):
    """Stands in for the history and profile stores."""
    import SpotiFLAC.core.history as history_module
    import SpotiFLAC.core.profiles as profiles_module

    saved: dict[str, dict] = {"weekend": dict(_SAVED_PROFILE)}

    monkeypatch.setattr(
        history_module,
        "get_recent_fetches",
        lambda: [
            {"url": "https://open.spotify.com/track/one", "label": "One"},
            {"url": "https://open.spotify.com/track/two", "label": "Two"},
        ],
    )

    async def _list():
        return sorted(saved)

    async def _get(name):
        return saved.get(name)

    async def _save(name, cfg):
        saved[name] = dict(cfg)

    async def _delete(name):
        return saved.pop(name, None) is not None

    monkeypatch.setattr(profiles_module, "list_profiles_async", _list)
    monkeypatch.setattr(profiles_module, "get_profile_async", _get)
    monkeypatch.setattr(profiles_module, "save_profile_async", _save)
    monkeypatch.setattr(profiles_module, "delete_profile_async", _delete)
    return saved


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


@drives_the_ui
async def test_history_and_profiles_are_listed(stub_session) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SESSION_INDEX
        await _settled(pilot)

        from textual.widgets import OptionList

        history = pilot.app.query_one("#history-list", OptionList)
        profiles = pilot.app.query_one("#profile-list", OptionList)

        assert history.option_count == 2
        assert profiles.option_count == 1
        assert profiles.get_option_at_index(0).id == "weekend"


@drives_the_ui
async def test_picking_a_url_fills_the_form(stub_session) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        panel = pilot.app.query_one("#session", SessionPanel)
        panel.post_message(
            SessionPanel.UrlChosen("https://open.spotify.com/track/two"),
        )
        await _settled(pilot)

        from textual.widgets import Input

        assert (
            pilot.app.query_one("#cfg-url", Input).value
            == "https://open.spotify.com/track/two"
        )
        assert pilot.app.state.url == "https://open.spotify.com/track/two"
        # Adopting a URL is a configuration act, so it lands you on the form.
        assert pilot.app.query_one("#panels").current == "download"


@drives_the_ui
async def test_loading_a_profile_replaces_every_setting(stub_session) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SESSION_INDEX
        await _settled(pilot)

        from textual.widgets import OptionList

        profiles = pilot.app.query_one("#profile-list", OptionList)
        profiles.highlighted = 0
        profiles.action_select()
        await _settled(pilot)

        state = pilot.app.state
        assert state.profile_loaded == "weekend"
        assert state.output_dir == "/tmp/from-profile"
        assert state.services == ["qobuz"]
        assert state.quality == "HI_RES"
        assert state.track_max_retries == 4

        # The form was rebuilt, not merely the state reassigned — a control
        # left showing the old profile is the failure this guards against.
        from textual.widgets import Input

        assert pilot.app.query_one("#cfg-output_dir", Input).value == "/tmp/from-profile"
        assert pilot.app.query_one("#panels").current == "download"


@drives_the_ui
async def test_saving_a_profile_stores_the_current_configuration(
    stub_session,
) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SESSION_INDEX
        await _settled(pilot)

        from textual.widgets import Button, Input

        pilot.app.query_one("#profile-name", Input).value = "nightly"
        pilot.app.query_one("#profile-save", Button).press()
        await _settled(pilot)

        assert "nightly" in stub_session
        assert stub_session["nightly"]["output_dir"] == "/tmp/spotiflac-test"
        assert stub_session["nightly"]["services"] == ["tidal"]


@drives_the_ui
async def test_deleting_a_profile_removes_it(stub_session) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SESSION_INDEX
        await _settled(pilot)

        from textual.widgets import Button, Input

        pilot.app.query_one("#profile-name", Input).value = "weekend"
        pilot.app.query_one("#profile-delete", Button).press()
        await _settled(pilot)

        assert "weekend" not in stub_session
        assert "Deleted" in str(pilot.app.query_one("#session-status").content)


@drives_the_ui
async def test_an_unreadable_store_is_reported_not_raised(monkeypatch) -> None:
    """A broken history file must not stop the UI from opening."""
    import SpotiFLAC.core.history as history_module
    import SpotiFLAC.core.profiles as profiles_module

    def _explode():
        msg = "history is corrupt"
        raise RuntimeError(msg)

    async def _explode_async():
        msg = "profiles are corrupt"
        raise RuntimeError(msg)

    monkeypatch.setattr(history_module, "get_recent_fetches", _explode)
    monkeypatch.setattr(profiles_module, "list_profiles_async", _explode_async)

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SESSION_INDEX
        await _settled(pilot)

        from textual.widgets import OptionList

        history = pilot.app.query_one("#history-list", OptionList)
        assert history.option_count == 1
        assert "Nothing fetched yet" in str(history.get_option_at_index(0).prompt)


@drives_the_ui
async def test_saving_without_a_name_says_so(stub_session) -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = _SESSION_INDEX
        await _settled(pilot)

        from textual.widgets import Button

        pilot.app.query_one("#profile-save", Button).press()
        await _settled(pilot)

        assert "Name the profile" in str(
            pilot.app.query_one("#session-status").content,
        )

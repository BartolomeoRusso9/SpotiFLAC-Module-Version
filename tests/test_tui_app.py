"""The TUI, driven headless.

Textual's `run_test()` runs the real app against an off-screen terminal, so
these are not "does it import" tests: the widgets are mounted, the CSS is
parsed, and the bindings fire. That matters most for the parts that are easy
to get subtly wrong and impossible to notice from a unit test — a stylesheet
that fails to parse, a widget id the panel queries but never mounts, a
`Select` handed a value that is not among its options.
"""

from __future__ import annotations

import asyncio
import functools

from SpotiFLAC.tui.app import MODES, THEMES, SpotiFLACTui
from SpotiFLAC.tui.config_state import ConfigState


def drives_the_ui(test):
    """Runs an async test body, the way the rest of this suite does.

    The project has no pytest-asyncio; every other async test here spells it
    `asyncio.run(...)`. One decorator keeps these readable without adding a
    plugin for twelve tests.
    """

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


@drives_the_ui
async def test_the_app_starts_and_stops_cleanly() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        assert pilot.app.query_one("#sidebar") is not None
        assert pilot.app.query_one("#panels") is not None
        # The log pane starts hidden: it is for a run, not for reading at rest.
        assert pilot.app.query_one("#log-pane").display is False


@drives_the_ui
async def test_the_stylesheet_is_found_and_applied() -> None:
    """A broken .tcss crashes at startup, so reaching here means it parsed.

    What that alone would not catch is the file going missing from an
    install — Textual would carry on with an unstyled screen — so this also
    checks the rules actually came from it.
    """
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        sources = " ".join(str(key) for key in pilot.app.stylesheet.source)
        assert "spotiflac.tcss" in sources

        sidebar = pilot.app.query_one("#sidebar")
        assert sidebar.styles.width is not None


@drives_the_ui
async def test_the_sidebar_switches_panels() -> None:
    """Indices come from MODES, so adding a mode cannot silently reorder this."""
    keys = [key for key, _label in MODES]

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        switcher = pilot.app.query_one("#panels")
        assert switcher.current == "download"

        for key in ("queue", "command"):
            pilot.app.query_one("#sidebar").index = keys.index(key)
            await pilot.pause()
            assert switcher.current == key


@drives_the_ui
async def test_every_sidebar_entry_has_a_panel() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        switcher = pilot.app.query_one("#panels")
        for index, (key, _label) in enumerate(MODES):
            pilot.app.query_one("#sidebar").index = index
            await pilot.pause()
            assert switcher.current == key
            # Would raise if the panel were listed but never mounted.
            assert pilot.app.query_one(f"#{key}") is not None


@drives_the_ui
async def test_editing_a_field_updates_the_state_and_the_command() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Input

        pilot.app.query_one("#cfg-output_dir", Input).value = "/tmp/elsewhere"
        await pilot.pause()

        assert pilot.app.state.output_dir == "/tmp/elsewhere"
        assert "/tmp/elsewhere" in pilot.app.state.cli_command()


@drives_the_ui
async def test_a_dependent_setting_is_disabled_rather_than_ignored() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Switch

        bitrate = pilot.app.query_one("#cfg-transcode_bitrate")
        # No conversion is configured, so a bitrate would do nothing.
        assert bitrate.disabled is True

        separator = pilot.app.query_one("#cfg-artist_separator")
        assert separator.disabled is False
        pilot.app.query_one("#cfg-first_artist_only", Switch).value = True
        await pilot.pause()
        assert separator.disabled is True


@drives_the_ui
async def test_an_unrunnable_command_is_generated_with_the_gaps_named() -> None:
    """The panel used to withhold the command until the run was complete.

    That made it useless for the thing it is best at — flipping options and
    watching which flag each one is. An incomplete state still produces a
    real command; what it must not do is pass an empty string off as an
    answer, so every unmet requirement shows as a placeholder and is listed
    above the command as well.
    """
    async with SpotiFLACTui(ConfigState()).run_test() as pilot:
        pilot.app.query_one("#sidebar").index = 2
        await pilot.pause()

        rendered = str(pilot.app.query_one("#command").content)
        assert "Not runnable yet" in rendered
        assert "spotiflac" in rendered
        # The gaps are named, never rendered as `spotiflac '' … -s`.
        assert "<URL-or-CSV>" in rendered
        assert "<PROVIDER>" in rendered
        assert "''" not in rendered


@drives_the_ui
async def test_the_command_panel_shows_the_command_once_it_can_run() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        rendered = str(pilot.app.query_one("#command").content)
        assert "spotiflac" in rendered
        assert "-s" in rendered


@drives_the_ui
async def test_the_log_pane_toggles() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pane = pilot.app.query_one("#log-pane")
        assert pane.display is False

        await pilot.press("ctrl+l")
        assert pane.display is True

        await pilot.press("ctrl+l")
        assert pane.display is False


@drives_the_ui
async def test_single_key_bindings_do_not_steal_typing() -> None:
    """`j`, `k`, `q` and `t` are bindings *and* ordinary letters.

    A binding marked `priority=True` fires before the focused widget sees the
    key, which would make four letters untypeable in every text field on the
    screen. None of these are, and this is what says so.
    """
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Input

        field = pilot.app.query_one("#cfg-url", Input)
        field.value = ""
        field.focus()
        await pilot.pause()

        for character in "jkqt":
            await pilot.press(character)
        await pilot.pause()

        assert field.value == "jkqt"
        assert pilot.app.is_running, "one of those letters quit the app"


@drives_the_ui
async def test_the_help_screen_opens_and_closes() -> None:
    from SpotiFLAC.tui.help_screen import HelpScreen

    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        # From the sidebar, so no text field swallows the question mark.
        pilot.app.query_one("#sidebar").focus()
        await pilot.pause()

        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, HelpScreen)


@drives_the_ui
async def test_the_help_screen_lists_the_real_bindings() -> None:
    """Documented keys must be keys the app actually binds."""
    from SpotiFLAC.tui.help_screen import KEYS

    bound = set()
    for binding in SpotiFLACTui.BINDINGS:
        bound.update(part.strip() for part in binding.key.split(","))

    documented = {
        "Ctrl+R": "ctrl+r",
        "Ctrl+C": "ctrl+c",
        "Ctrl+L": "ctrl+l",
        "/": "slash",
        "j / k": "j",
        "t": "t",
        "?": "question_mark",
        "q": "q",
    }
    for label, _what in KEYS:
        key = documented.get(label)
        if key is not None:
            assert key in bound, f"help lists {label!r}, which nothing binds"


@drives_the_ui
async def test_themes_cycle() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        assert pilot.app.theme == THEMES[0]
        await pilot.press("t")
        assert pilot.app.theme == THEMES[1]


@drives_the_ui
async def test_starting_a_run_without_the_essentials_says_what_is_missing() -> None:
    async with SpotiFLACTui(ConfigState()).run_test() as pilot:
        pilot.app.action_start_download()
        await pilot.pause()

        status = str(pilot.app.query_one("#status").content)
        assert "Cannot start" in status
        assert "a URL or a CSV track list" in status
        # Not the folder: that one has a default, so it is never missing.
        assert "a destination folder" not in status
        assert pilot.app._download_running is False


@drives_the_ui
async def test_quitting_is_refused_while_a_download_runs() -> None:
    """`_download_running`, not `_running` — Textual owns the latter."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app._download_running = True
        pilot.app.action_request_quit()
        await pilot.pause()

        assert "A download is running" in str(pilot.app.query_one("#status").content)
        pilot.app._download_running = False

"""The TUI, driven headless.

Textual's `run_test()` runs the real app against an off-screen terminal, so
these are not "does it import" tests: the widgets are mounted, the CSS is
parsed, and the bindings fire. That matters most for the parts that are easy
to get subtly wrong and impossible to notice from a unit test — a stylesheet
that fails to parse, a widget id the panel queries but never mounts, a
`Select` handed a value that is not among its options.
"""

from __future__ import annotations


import pytest
from tui_harness import app_of, drives_the_ui

from mornye.tui.app import MODES, THEMES, MornyeTui
from mornye.tui.config_state import ConfigState


def _ready_state() -> ConfigState:
    return ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/mornye-test",
        services=["tidal"],
    )


def _record_and_succeed(target: list[str], text: str) -> bool:
    target.append(text)
    return True


@drives_the_ui
async def test_the_app_starts_and_stops_cleanly() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        assert app_of(pilot).query_one("#sidebar") is not None
        assert app_of(pilot).query_one("#panels") is not None
        # The log pane starts hidden: it is for a run, not for reading at rest.
        assert app_of(pilot).query_one("#log-pane").display is False


@drives_the_ui
async def test_the_stylesheet_is_found_and_applied() -> None:
    """A broken .tcss crashes at startup, so reaching here means it parsed.

    What that alone would not catch is the file going missing from an
    install — Textual would carry on with an unstyled screen — so this also
    checks the rules actually came from it.
    """
    async with MornyeTui(_ready_state()).run_test() as pilot:
        sources = " ".join(str(key) for key in app_of(pilot).stylesheet.source)
        assert "mornye.tcss" in sources

        sidebar = app_of(pilot).query_one("#sidebar")
        assert sidebar.styles.width is not None


@drives_the_ui
async def test_the_sidebar_switches_panels() -> None:
    """Indices come from MODES, so adding a mode cannot silently reorder this."""
    keys = [key for key, _label in MODES]

    async with MornyeTui(_ready_state()).run_test() as pilot:
        switcher = app_of(pilot).query_one("#panels")
        assert switcher.current == "download"

        for key in ("queue", "command"):
            app_of(pilot).query_one("#sidebar").index = keys.index(key)
            await pilot.pause()
            assert switcher.current == key


@drives_the_ui
async def test_every_sidebar_entry_has_a_panel() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        switcher = app_of(pilot).query_one("#panels")
        for index, (key, _label) in enumerate(MODES):
            app_of(pilot).query_one("#sidebar").index = index
            await pilot.pause()
            assert switcher.current == key
            # Would raise if the panel were listed but never mounted.
            assert app_of(pilot).query_one(f"#{key}") is not None


@drives_the_ui
async def test_editing_a_field_updates_the_state_and_the_command() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Input

        app_of(pilot).query_one("#cfg-output_dir", Input).value = "/tmp/elsewhere"
        await pilot.pause()

        assert app_of(pilot).state.output_dir == "/tmp/elsewhere"
        assert "/tmp/elsewhere" in app_of(pilot).state.cli_command()


@drives_the_ui
async def test_animated_artwork_api_url_is_editable_and_reaches_the_command() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Input, Switch

        api_url = app_of(pilot).query_one(
            "#cfg-animated_artwork_api_url",
            Input,
        )
        api_url.value = "https://artwork.example.test"
        app_of(pilot).query_one("#cfg-save_animated_artwork", Switch).value = True
        await pilot.pause()

        assert app_of(pilot).state.animated_artwork_api_url == (
            "https://artwork.example.test"
        )
        command = app_of(pilot).state.cli_command()
        assert "--animated-artwork" in command
        assert "--animated-artwork-api-url https://artwork.example.test" in command


@drives_the_ui
async def test_a_change_before_the_command_panel_exists_is_not_fatal() -> None:
    """Regression (seen on Windows CI): the form's Changed can reach the app
    before the command panel, composed after it, is mounted — and
    `query_one("#command")` then raised NoMatches, killing the app."""
    async with MornyeTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Input

        await app_of(pilot).query_one("#command").remove()
        app_of(pilot).query_one("#cfg-output_dir", Input).value = "/tmp/elsewhere"
        await pilot.pause()

        assert app_of(pilot).state.output_dir == "/tmp/elsewhere"


@drives_the_ui
async def test_a_dependent_setting_is_disabled_rather_than_ignored() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Switch

        bitrate = app_of(pilot).query_one("#cfg-transcode_bitrate")
        # No conversion is configured, so a bitrate would do nothing.
        assert bitrate.disabled is True

        separator = app_of(pilot).query_one("#cfg-artist_separator")
        assert separator.disabled is False
        app_of(pilot).query_one("#cfg-first_artist_only", Switch).value = True
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
    async with MornyeTui(ConfigState()).run_test() as pilot:
        app_of(pilot).query_one("#sidebar").index = 2
        await pilot.pause()

        rendered = str(app_of(pilot).query_one("#command").content)
        assert "Not runnable yet" in rendered
        assert "mornye" in rendered
        # The gaps are named, never rendered as `mornye '' … -s`.
        assert "<URL-or-CSV>" in rendered
        assert "<PROVIDER>" in rendered
        assert "''" not in rendered


@drives_the_ui
async def test_the_command_panel_shows_the_command_once_it_can_run() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        rendered = str(app_of(pilot).query_one("#command").content)
        assert "mornye" in rendered
        assert "-s" in rendered


@drives_the_ui
async def test_the_log_pane_toggles() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        pane = app_of(pilot).query_one("#log-pane")
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
    async with MornyeTui(_ready_state()).run_test() as pilot:
        from textual.widgets import Input

        field = app_of(pilot).query_one("#cfg-url", Input)
        field.value = ""
        field.focus()
        await pilot.pause()

        for character in "jkqt":
            await pilot.press(character)
        await pilot.pause()

        assert field.value == "jkqt"
        assert app_of(pilot).is_running, "one of those letters quit the app"


@drives_the_ui
async def test_the_help_screen_opens_and_closes() -> None:
    from mornye.tui.help_screen import HelpScreen

    async with MornyeTui(_ready_state()).run_test() as pilot:
        # From the sidebar, so no text field swallows the question mark.
        app_of(pilot).query_one("#sidebar").focus()
        await pilot.pause()

        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app_of(pilot).screen, HelpScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app_of(pilot).screen, HelpScreen)


@drives_the_ui
async def test_the_help_screen_lists_the_real_bindings() -> None:
    """Documented keys must be keys the app actually binds."""
    from mornye.tui.help_screen import KEYS

    from textual.binding import Binding

    bound: set[str] = set()
    for binding in MornyeTui.BINDINGS:
        if not isinstance(binding, Binding):
            continue
        bound.update(part.strip() for part in binding.key.split(","))

    documented = {
        "Ctrl+R": "ctrl+r",
        "Ctrl+C": "ctrl+c",
        "Ctrl+L": "ctrl+l",
        "Ctrl+O": "ctrl+o",
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
async def test_ctrl_o_copies_the_whole_log_as_written() -> None:
    """Every kept line, unwrapped, whichever panel has focus and whether or
    not the log pane is open."""
    async with MornyeTui(_ready_state()).run_test(size=(60, 30)) as pilot:
        copied: list[str] = []
        app_of(pilot).copy_to_clipboard = copied.append
        long_line = "[tidal] saved /music/The Weeknd/After Hours/" + "x" * 80 + ".flac"
        app_of(pilot)._write_log("[RUN] 1 track(s) · tidal")
        app_of(pilot)._write_log(long_line, "info")
        assert app_of(pilot).query_one("#log-pane").display is False

        app_of(pilot).query_one("#sidebar").focus()
        await pilot.press("ctrl+o")
        await pilot.pause()

        assert copied == ["[RUN] 1 track(s) · tidal\n" + long_line]
        status = str(app_of(pilot).query_one("#status").content)
        assert "2 line(s)" in status
        # No system clipboard in the suite (conftest), so the status must not
        # claim a copy it cannot vouch for.
        assert "OSC 52" in status


@drives_the_ui
async def test_ctrl_o_also_puts_the_log_on_the_system_clipboard(monkeypatch) -> None:
    """macOS Terminal ignores OSC 52 — the report was "non copia". The
    system clipboard is filled as well whenever a copy command exists."""
    from mornye.tui import clipboard

    native: list[str] = []
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(
        clipboard, "native_copy", lambda text: _record_and_succeed(native, text)
    )

    async with MornyeTui(_ready_state()).run_test() as pilot:
        osc: list[str] = []
        app_of(pilot).copy_to_clipboard = osc.append
        app_of(pilot)._write_log("one")
        app_of(pilot)._write_log("two")
        await pilot.press("ctrl+o")
        await pilot.pause()

        assert osc == ["one\ntwo"]
        assert native == ["one\ntwo"]
        status = str(app_of(pilot).query_one("#status").content)
        assert "Log copied — 2 line(s)" in status
        assert "OSC 52" not in status


@drives_the_ui
async def test_ctrl_y_takes_the_same_two_routes(monkeypatch) -> None:
    from mornye.tui import clipboard

    native: list[str] = []
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(
        clipboard, "native_copy", lambda text: _record_and_succeed(native, text)
    )

    async with MornyeTui(_ready_state()).run_test() as pilot:
        app_of(pilot).copy_to_clipboard = lambda text: None
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert native and native[0].startswith("mornye")
        assert "Command copied" in str(app_of(pilot).query_one("#status").content)


def test_over_ssh_only_the_terminal_route_is_used(monkeypatch) -> None:
    """The system clipboard there is the server's, not the user's."""
    from types import SimpleNamespace

    from mornye.tui import clipboard

    called: list[str] = []
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 51000 10.0.0.1 22")
    monkeypatch.setattr(
        clipboard, "native_copy", lambda text: _record_and_succeed(called, text)
    )
    osc: list[str] = []
    assert (
        clipboard.copy_text(SimpleNamespace(copy_to_clipboard=osc.append), "x") is False
    )
    assert osc == ["x"] and called == []


@pytest.mark.uses_system_clipboard
def test_the_copy_command_follows_the_platform(monkeypatch) -> None:
    from mornye.tui import clipboard

    present = {"pbcopy", "xclip", "wl-copy", "clip"}
    monkeypatch.setattr(
        clipboard.shutil, "which", lambda name: name if name in present else None
    )

    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    assert clipboard._native_command() == (["pbcopy"], "utf-8")

    monkeypatch.setattr(clipboard.sys, "platform", "win32")
    assert clipboard._native_command() == (["clip"], "utf-16")

    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert clipboard._native_command() == (["wl-copy"], "utf-8")

    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("DISPLAY", ":0")
    assert clipboard._native_command() == (
        ["xclip", "-selection", "clipboard"],
        "utf-8",
    )

    monkeypatch.delenv("DISPLAY")
    assert clipboard._native_command() is None


@drives_the_ui
async def test_ctrl_o_on_an_empty_log_says_so_and_copies_nothing() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        copied: list[str] = []
        app_of(pilot).copy_to_clipboard = copied.append
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert copied == []
        assert "empty" in str(app_of(pilot).query_one("#status").content)


def test_the_copied_log_is_capped_like_the_pane() -> None:
    from mornye.tui import app as app_module

    tui = MornyeTui(_ready_state())
    assert tui._log_lines.maxlen == app_module._LOG_MAX_LINES


@drives_the_ui
async def test_themes_cycle() -> None:
    async with MornyeTui(_ready_state()).run_test() as pilot:
        assert app_of(pilot).theme == THEMES[0]
        await pilot.press("t")
        assert app_of(pilot).theme == THEMES[1]


@drives_the_ui
async def test_starting_a_run_without_the_essentials_says_what_is_missing() -> None:
    async with MornyeTui(ConfigState()).run_test() as pilot:
        app_of(pilot).action_start_download()
        await pilot.pause()

        status = str(app_of(pilot).query_one("#status").content)
        assert "Cannot start" in status
        assert "a URL or a CSV track list" in status
        # Not the folder: that one has a default, so it is never missing.
        assert "a destination folder" not in status
        assert app_of(pilot)._download_running is False


@drives_the_ui
async def test_quitting_is_refused_while_a_download_runs() -> None:
    """`_download_running`, not `_running` — Textual owns the latter."""
    async with MornyeTui(_ready_state()).run_test() as pilot:
        app_of(pilot)._download_running = True
        app_of(pilot).action_request_quit()
        await pilot.pause()

        assert "A download is running" in str(
            app_of(pilot).query_one("#status").content
        )
        app_of(pilot)._download_running = False

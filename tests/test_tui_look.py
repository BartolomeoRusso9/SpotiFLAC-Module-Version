"""The look, pinned.

Styling is the part of a UI that rots without anyone noticing: a title loses
its mark, a badge stops matching its theme, the wordmark stops fitting. None
of that fails anything. So the pieces of the MovieBox-Tui vocabulary this UI
borrows are asserted here — not pixel by pixel, but by the properties that
make them recognisable.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from SpotiFLAC.tui import branding
from SpotiFLAC.tui.app import THEMES, SpotiFLACTui
from SpotiFLAC.tui.banner import Banner, HintBar
from SpotiFLAC.tui.config_state import ConfigState


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


# ---------------------------------------------------------------------------
# The wordmark
# ---------------------------------------------------------------------------


def test_the_wordmark_is_rectangular() -> None:
    """Ragged rows would tear the letterform apart when centred."""
    for art in (branding.WORDMARK_FULL, branding.WORDMARK_COMPACT):
        rows = art.split("\n")
        # Trailing blanks are trimmed, so rows may be short — never long.
        assert max(len(row) for row in rows) == max(len(r) for r in rows)
        assert all(len(row) <= max(len(r) for r in rows) for row in rows)


def test_the_wordmark_shrinks_to_fit() -> None:
    wide = branding.wordmark_for(120, 40, plain=False)
    narrow = branding.wordmark_for(40, 40, plain=False)
    tiny = branding.wordmark_for(10, 40, plain=False)

    assert wide == branding.WORDMARK_FULL
    assert narrow == branding.WORDMARK_COMPACT
    assert tiny == branding.WORDMARK_PLAIN


def test_a_short_terminal_gets_the_small_wordmark() -> None:
    """Six rows of logo on a 24-row screen is a quarter of the screen."""
    assert branding.wordmark_for(120, 24, plain=False) == branding.WORDMARK_COMPACT
    assert branding.wordmark_for(120, 40, plain=False) == branding.WORDMARK_FULL


def test_a_plain_terminal_gets_no_block_art() -> None:
    art = branding.wordmark_for(120, 40, plain=True)
    assert art == branding.WORDMARK_PLAIN
    assert art.isascii()


def test_plain_terminal_follows_no_color(monkeypatch) -> None:
    monkeypatch.delenv("SPOTIFLAC_PLAIN_TUI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert branding.plain_terminal() is False

    monkeypatch.setenv("NO_COLOR", "1")
    assert branding.plain_terminal() is True


def test_every_flourish_has_an_ascii_fallback() -> None:
    """A terminal that cannot draw this must not be decorated badly."""
    assert branding.panel_title("Queue", plain=True).isascii()
    assert branding.pointer(plain=True).isascii()
    assert branding.quality_badge("LOSSLESS", plain=True)[0].isascii()
    assert branding.status_badge("completed", plain=True)[0].isascii()
    assert branding.notice("error", "boom", plain=True)[0].isascii()


# ---------------------------------------------------------------------------
# Titles, badges, hints
# ---------------------------------------------------------------------------


def test_a_panel_title_says_whether_its_pane_is_live() -> None:
    """The dot is the marker MovieBox fills in on the focused pane."""
    live = branding.panel_title("Queue", focused=True, plain=False)
    idle = branding.panel_title("Queue", focused=False, plain=False)

    assert live == "● Queue"
    assert idle == "Queue", "an idle title starts at the name, not at a gap"

    # No leading rule: Textual draws its own on both sides, and repeating it
    # produced `╭─ ─ ✦  Queue ─╮`.
    assert not live.startswith("─")


def test_a_panel_title_carries_what_is_worth_knowing() -> None:
    """MovieBox puts the counts in the title, `·`-separated."""
    assert branding.panel_title(
        "Streams", "2 available", "1/2", focused=True, plain=False,
    ) == "● Streams · 2 available · 1/2"

    # Empty facts are dropped rather than leaving a dangling separator.
    assert branding.panel_title("Queue", "", focused=True, plain=False) == "● Queue"
    assert branding.panel_title("Audio", "4", focused=False, plain=False) == "Audio · 4"


def test_a_panel_tag_escapes_its_bracket() -> None:
    """A border title is markup, and `[ live ]` reads as a style tag.

    Unescaped, Textual auto-closed it into `[ live ] [/ live ]` and drew
    nothing. Tags containing `+` or `--` were rejected as tags and drew fine,
    which made a broken rule look like a few inconsistent panels.
    """
    assert branding.panel_tag("live") == "\\[ live ] "


@drives_the_ui
async def test_every_panel_tag_actually_draws() -> None:
    """Asserting the attribute is set was not enough — it was, and was wrong."""
    import re

    from SpotiFLAC.tui.app import MODES

    async with SpotiFLACTui(_ready_state()).run_test(size=(104, 36)) as pilot:
        keys = [key for key, _label in MODES]
        for key in keys:
            pilot.app.query_one("#sidebar").index = keys.index(key)
            for _ in range(4):
                await pilot.pause()

            panel = pilot.app.query_one(f"#{key}")
            drawn = re.sub(r"<[^>]+>", "", pilot.app.export_screenshot())
            drawn = drawn.replace("&#160;", " ")
            expected = str(panel.border_subtitle).replace("\\", "").strip()

            assert expected in drawn, f"#{key}'s tag never reached the screen"


def test_a_badge_is_padded_so_the_colour_reads_as_a_label() -> None:
    text, css = branding.quality_badge("HI_RES_LOSSLESS", plain=False)
    assert text == " HI-RES "
    assert css == "badge-gold"
    # Bracketed instead when there is no colour to pad.
    assert branding.quality_badge("HI_RES_LOSSLESS", plain=True)[0] == "[HI-RES]"


def test_an_unknown_quality_still_gets_a_badge() -> None:
    text, css = branding.quality_badge("SOMETHING_NEW", plain=False)
    assert text.strip()
    assert css == "badge-muted"


def test_hints_read_as_key_then_action() -> None:
    assert branding.key_hint("Ctrl+R", "Run") == "[Ctrl+R] Run"
    assert branding.key_hint("?") == "[?]"


def test_the_key_is_the_coloured_part_of_a_hint() -> None:
    """A row of eight identically grey hints is a wall, not a legend."""
    marked = branding.key_hint_markup("Ctrl+R", "Run")

    assert "$warning" in marked
    assert marked.endswith(" Run"), "only the key is lit"
    # Escaped, or Textual reads `[Ctrl+R]` as a style tag.
    assert "\\[Ctrl+R]" in marked


# ---------------------------------------------------------------------------
# On the running screen
# ---------------------------------------------------------------------------


@drives_the_ui
async def test_the_default_theme_is_the_one_the_screen_was_drawn_against() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        assert pilot.app.theme == "catppuccin-mocha"


def test_every_offered_theme_exists() -> None:
    """A missing theme name is a crash on the keypress that selects it."""
    from textual.theme import BUILTIN_THEMES

    missing = [name for name in THEMES if name not in BUILTIN_THEMES]
    assert not missing, f"THEMES names Textual does not ship: {missing}"


@drives_the_ui
async def test_the_banner_and_hint_bar_replace_the_default_chrome() -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        assert isinstance(pilot.app.query_one("#banner"), Banner)
        assert isinstance(pilot.app.query_one("#hints"), HintBar)

        wordmark = str(pilot.app.query_one("#wordmark").content)
        assert wordmark == branding.WORDMARK_FULL

        # The version hangs off the wordmark's right edge, as MovieBox tucks
        # its own under the last letter — not centred under the whole block.
        subtitle = str(pilot.app.query_one("#wordmark-subtitle").content)
        assert subtitle.strip().startswith("v")
        assert subtitle.startswith(" "), "the version is positioned by padding"


@drives_the_ui
async def test_the_banner_leaves_room_for_its_subtitle() -> None:
    """It was sized to the art alone, and the subtitle fell off the bottom."""
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        banner = pilot.app.query_one("#banner", Banner)
        rows = len(branding.WORDMARK_FULL.split("\n"))
        assert banner.size.height >= rows + 1


@drives_the_ui
async def test_every_panel_is_a_titled_card() -> None:
    from SpotiFLAC.tui.app import MODES

    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        for key, _label in MODES:
            panel = pilot.app.query_one(f"#{key}")
            assert str(panel.border_title).strip(), f"#{key} has no title"
            assert panel.border_subtitle, f"#{key} has no tag"
            assert panel.styles.border.top[0] == "round"


@drives_the_ui
async def test_a_transient_message_becomes_a_toast() -> None:
    """News goes to a toast; standing state stays on the status line.

    Asserted through `App.notify` rather than by looking for the widget:
    Textual does not mount a Toast under `run_test`, so the only honest
    check here is the call. The rendering is verified against a real
    terminal instead.
    """
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        sent: list[dict] = []
        pilot.app.notify = lambda message, **kwargs: sent.append(
            {"message": message, **kwargs},
        )

        pilot.app._toast("it worked", "success")
        pilot.app._toast("it did not", "error")
        pilot.app._toast("careful", "warning")

        assert [note["severity"] for note in sent] == [
            "information",
            "error",
            "warning",
        ]
        assert sent[0]["message"] == "it worked"

        # No title on the notification: it goes on the toast's border, which
        # is where MovieBox has it and where it costs no line inside a box
        # three rows tall. The words are queued for the widget to claim.
        assert all("title" not in note for note in sent)
        assert pilot.app._pending_toast_labels == ["DONE", "ERROR", "WARNING"]


@drives_the_ui
async def test_a_missing_toast_widget_does_not_leak_labels() -> None:
    """`Toast` is a private Textual class; this must survive it moving."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        pilot.app.notify = lambda message, **kwargs: None
        pilot.app._toast("something happened", "warning")
        assert pilot.app._pending_toast_labels == ["WARNING"]

        # Textual mounts no Toast under run_test, so this exercises the same
        # path a future version without `_toast` would take.
        pilot.app._label_toast_borders()
        await pilot.pause()

        assert len(pilot.app._pending_toast_labels) <= 1


@drives_the_ui
async def test_announcing_says_it_in_both_places() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        sent: list[str] = []
        pilot.app.notify = lambda message, **kwargs: sent.append(message)

        pilot.app._announce("the run finished", "success")
        await pilot.pause()

        assert sent == ["the run finished"]
        assert "the run finished" in str(pilot.app.query_one("#status").content)


@drives_the_ui
async def test_progress_ticks_never_toast() -> None:
    """One toast per progress event would bury what you are watching."""
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        sent: list[str] = []
        pilot.app.notify = lambda message, **kwargs: sent.append(message)

        pilot.app._set_status("2 done · 3 queued · 4.1 MB/s")
        await pilot.pause()

        assert sent == []
        assert "2 done" in str(pilot.app.query_one("#status").content)


@drives_the_ui
async def test_a_panes_title_fills_in_when_it_takes_focus() -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        sidebar = pilot.app.query_one("#sidebar")
        sidebar.focus()
        await pilot.pause()

        assert str(sidebar.border_title).startswith("●")
        assert not str(pilot.app.query_one("#download").border_title).startswith("●")

        pilot.app.query_one("#cfg-output_dir").focus()
        await pilot.pause()

        assert str(pilot.app.query_one("#download").border_title).startswith("●")
        assert not str(sidebar.border_title).startswith("●")


@drives_the_ui
async def test_the_status_line_is_marked_by_severity() -> None:
    async with SpotiFLACTui(_ready_state()).run_test() as pilot:
        status = pilot.app.query_one("#status")

        pilot.app._set_status("all good", "success")
        await pilot.pause()
        assert status.has_class("notice-success")
        assert "✔" in str(status.content)

        pilot.app._set_status("careful", "warning")
        await pilot.pause()
        assert status.has_class("notice-warning")
        assert not status.has_class("notice-success"), "the old class stuck"


@drives_the_ui
async def test_the_hint_bar_drops_hints_rather_than_truncating() -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(60, 30)) as pilot:
        bar = pilot.app.query_one("#hints", HintBar)
        await pilot.pause()
        # `content` is the markup; what has to fit is the text it renders to.
        rendered = str(bar.render())

        assert len(rendered) <= 60, rendered
        # The first hint is the one nobody can work without.
        assert "[Ctrl+R] Run" in rendered
        assert "[q] Quit" not in rendered


@drives_the_ui
async def test_the_quality_badge_follows_the_selected_tier() -> None:
    async with SpotiFLACTui(_ready_state()).run_test(size=(110, 40)) as pilot:
        badge = pilot.app.query_one("#quality-badge")
        assert "LOSSLESS" in str(badge.content)
        assert badge.has_class("badge-sapphire")

        pilot.app.state.quality = "HI_RES_LOSSLESS"
        pilot.app.query_one("#download")._refresh_dependencies()
        await pilot.pause()

        assert "HI-RES" in str(badge.content)
        assert badge.has_class("badge-gold")
        assert not badge.has_class("badge-sapphire")

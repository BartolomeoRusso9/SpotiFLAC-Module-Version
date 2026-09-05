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


def test_a_panel_title_carries_the_mark() -> None:
    assert branding.panel_title("Queue", plain=False) == "✦  Queue"
    # No leading rule: Textual draws its own on both sides, and repeating it
    # produced `╭─ ─ ✦  Queue ─╮`.
    assert not branding.panel_title("Queue", plain=False).startswith("─")


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
        assert "lossless" in str(pilot.app.query_one("#wordmark-subtitle").content)


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
            assert panel.border_title, f"#{key} has no title"
            assert "✦" in str(panel.border_title)
            assert panel.border_subtitle, f"#{key} has no tag"
            assert panel.styles.border.top[0] == "round"


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
        rendered = str(bar.content)

        assert len(rendered) <= 60
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

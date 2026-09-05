"""branding.py — The TUI's visual vocabulary, in one place.

The look is modelled on MovieBox-Tui (Rust + Ratatui), which the plan names
as the reference. Four things carry that identity, and they are all here so
that no panel has to reinvent one of them:

* **The wordmark** — a centred ANSI-Shadow block letterform, with a two-row
  fallback for narrow terminals and a plain one for terminals that can draw
  neither.
* **Decorated panel titles** — ``─ ✦  Providers`` on the left, an accent tag
  like ``[ /browse ]`` on the right. It is the single cheapest thing that
  makes a bordered box read as designed rather than as a default.
* **Badges** — a short label on a solid colour, the way a resolution is shown
  in MovieBox. Here they carry audio quality and per-track outcome.
* **Key hints** — ``[Ctrl+R] Run``, dim, along the bottom.

Everything degrades. `plain_terminal()` answers the question MovieBox calls
`basic_terminal`, and every helper takes the answer into account: box glyphs
become ASCII, filled badges become bracketed text, the wordmark becomes a
word. A terminal that cannot draw this should still be usable, not decorated
badly.
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def plain_terminal() -> bool:
    """Whether to fall back to ASCII and drop the colour flourishes.

    Three signals, all of them things a user or an environment sets on
    purpose: ``NO_COLOR`` (the convention), ``TERM=dumb`` (no capabilities at
    all), and ``SPOTIFLAC_PLAIN_TUI`` for anyone who simply prefers it.
    """
    if os.getenv("SPOTIFLAC_PLAIN_TUI", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if os.getenv("NO_COLOR") is not None:
        return True
    return os.getenv("TERM", "").strip().lower() in {"dumb", ""}


# ---------------------------------------------------------------------------
# The wordmark
# ---------------------------------------------------------------------------

#: ANSI Shadow, the same letterform MovieBox uses. 69 columns wide.
WORDMARK_FULL = r"""
███████╗ ██████╗  ██████╗ ████████╗██╗███████╗██╗      █████╗  ██████╗
██╔════╝ ██╔══██╗██╔═══██╗╚══██╔══╝██║██╔════╝██║     ██╔══██╗██╔════╝
███████╗ ██████╔╝██║   ██║   ██║   ██║█████╗  ██║     ███████║██║
╚════██║ ██╔═══╝ ██║   ██║   ██║   ██║██╔══╝  ██║     ██╔══██║██║
███████║ ██║     ╚██████╔╝   ██║   ██║██║     ███████╗██║  ██║╚██████╗
╚══════╝ ╚═╝      ╚═════╝    ╚═╝   ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝ ╚═════╝
""".strip("\n")

#: Two rows of half blocks, for when the full one will not fit. 33 columns.
WORDMARK_COMPACT = "\n".join(
    (
        "█▀▀ █▀█ █▀█ ▀█▀ █ █▀▀ █   █▀█ █▀▀",
        "▄▄█ █▀▀ █▄█  █  █ █▀▀ █▄▄ █▀█ █▄▄",
    ),
)

#: What is left when the terminal can draw neither.
WORDMARK_PLAIN = "S P O T I F L A C"

WORDMARK_FULL_WIDTH = max(len(line) for line in WORDMARK_FULL.split("\n"))
WORDMARK_COMPACT_WIDTH = max(len(line) for line in WORDMARK_COMPACT.split("\n"))

TAGLINE = "lossless, from the terminal"


def wordmark_for(width: int, height: int = 24, *, plain: bool | None = None) -> str:
    """The largest wordmark that fits, given the room available.

    Height matters as much as width: six rows of letterform on a 24-row
    terminal is a quarter of the screen spent on a logo, which is generous on
    a landing page and rude on a form you are trying to fill in.
    """
    if plain is None:
        plain = plain_terminal()
    if plain:
        return WORDMARK_PLAIN
    if width >= WORDMARK_FULL_WIDTH and height >= 30:
        return WORDMARK_FULL
    if width >= WORDMARK_COMPACT_WIDTH:
        return WORDMARK_COMPACT
    return WORDMARK_PLAIN


def wordmark_height(width: int, height: int = 24, *, plain: bool | None = None) -> int:
    return len(wordmark_for(width, height, plain=plain).split("\n"))


# ---------------------------------------------------------------------------
# Panel titles
# ---------------------------------------------------------------------------

#: Glyph pairs: (fancy, plain). The rule bookends a title; the mark is the
#: little star MovieBox puts before a card's name.
_RULE = ("─", "-")
_MARK = ("✦", "*")
_POINTER = ("·", "-")
#: The filled dot on the live pane's title.
_FOCUS_MARK = ("●", "*")
#: The bar down the left of the selected row.
_SELECTION_BAR = ("▎", ">")


def glyph(fancy: str, fallback: str, *, plain: bool | None = None) -> str:
    if plain is None:
        plain = plain_terminal()
    return fallback if plain else fancy


def panel_title(
    text: str,
    *facts: str,
    focused: bool = False,
    plain: bool | None = None,
) -> str:
    """``● Streams · 2 available`` — a title that says which pane is live.

    MovieBox writes its panel titles as a marker, a name, and then whatever
    is worth knowing about the contents, `·`-separated: how many there are,
    which one you are on. The marker is the part that carries focus — the
    live pane gets a filled dot, the others lose it and go dim — so you can
    tell where the keyboard is pointing without hunting for a highlight.

    No leading rule, unlike MovieBox: ratatui drops a title onto the border
    line at the corner so a leading ``─`` continues the box, while Textual
    insets its titles and draws the rule on both sides already. Repeating it
    produced ``╭─ ─ ✦  Name ─╮``.
    """
    parts = " · ".join([text, *[fact for fact in facts if fact]])
    if not focused:
        # No marker at all, not a blank one: MovieBox's idle titles start at
        # the name, and a placeholder space would leave `─   Audio` sitting
        # oddly far from the corner.
        return parts
    return f"{glyph(*_FOCUS_MARK, plain=plain)} {parts}"


def panel_tag(command: str) -> str:
    """The right-hand tag: ``[ /browse ]``, in MovieBox's accent style.

    The opening bracket is escaped because a border title is parsed as
    markup: `[ live ]` reads as a style tag, and Textual auto-closes it into
    `[ live ] [/ live ]`, which is both wrong and — since no such style
    exists — invisible. Tags with a `+` or `--` in them happened to be
    rejected as tags and drew correctly, which is what made this look like a
    handful of panels being inconsistent rather than one rule being broken.
    """
    return f"\\[ {command} ] "


def pointer(*, plain: bool | None = None) -> str:
    return glyph(*_POINTER, plain=plain)


# ---------------------------------------------------------------------------
# Badges
# ---------------------------------------------------------------------------

#: label → the CSS class that paints it. The classes live in the stylesheet
#: so a badge follows the theme rather than a hard-coded colour, which is
#: what MovieBox gets by reading its palette instead of literals.
QUALITY_BADGES: dict[str, tuple[str, str]] = {
    "HI_RES_LOSSLESS": ("HI-RES", "badge-gold"),
    "HI_RES": ("24-BIT", "badge-gold"),
    "DOLBY_ATMOS": ("ATMOS", "badge-lavender"),
    "LOSSLESS": ("LOSSLESS", "badge-sapphire"),
    "HIGH": ("HIGH", "badge-teal"),
    "LOW": ("LOW", "badge-muted"),
}

STATUS_BADGES: dict[str, tuple[str, str, str]] = {
    # status -> (fancy label, plain label, css class)
    "queued": ("QUEUED", "QUEUED", "badge-muted"),
    "downloading": ("↓ ACTIVE", "ACTIVE", "badge-sapphire"),
    "completed": ("✓ DONE", "DONE", "badge-success"),
    "failed": ("✗ FAILED", "FAILED", "badge-error"),
    "skipped": ("⏭ SKIPPED", "SKIPPED", "badge-muted"),
}


def quality_badge(quality: str, *, plain: bool | None = None) -> tuple[str, str]:
    """(markup, css class) for a quality tier."""
    label, css = QUALITY_BADGES.get(
        (quality or "").upper(),
        ((quality or "?").upper()[:9], "badge-muted"),
    )
    return badge_text(label, plain=plain), css


def status_badge(status: str, *, plain: bool | None = None) -> tuple[str, str]:
    """(markup, css class) for a track's outcome."""
    fancy, plain_label, css = STATUS_BADGES.get(
        (status or "").lower(),
        ((status or "?").upper()[:9], (status or "?").upper()[:9], "badge-muted"),
    )
    if plain is None:
        plain = plain_terminal()
    return badge_text(plain_label if plain else fancy, plain=plain), css


def badge_text(label: str, *, plain: bool | None = None) -> str:
    """A badge's text: padded for a filled pill, bracketed when it cannot be.

    The spaces are the badge — a background colour flush against the text
    reads as a highlight, not a label.
    """
    if plain is None:
        plain = plain_terminal()
    return f"[{label}]" if plain else f" {label} "


# ---------------------------------------------------------------------------
# Key hints
# ---------------------------------------------------------------------------


def key_hint(key: str, action: str = "") -> str:
    """``[Ctrl+R] Run`` as plain text — for measuring, and for plain mode."""
    return f"[{key}]" if not action else f"[{key}] {action}"


def key_hint_markup(key: str, action: str = "") -> str:
    """The same, with the key picked out in the accent colour.

    MovieBox colours the bracketed key and leaves the verb dim, which is what
    makes a row of eight hints scannable instead of a wall of grey. The
    brackets are escaped because Textual reads them as markup otherwise.
    """
    if plain_terminal():
        return key_hint(key, action)
    lit = f"[$warning]\\[{key}][/]"
    return lit if not action else f"{lit} {action}"


def hint_bar(*pairs: tuple[str, str], separator: str = "   ") -> str:
    return separator.join(key_hint(key, action) for key, action in pairs)


def hint_bar_markup(*pairs: tuple[str, str], separator: str = "   ") -> str:
    return separator.join(key_hint_markup(key, action) for key, action in pairs)


def selection_bar(*, plain: bool | None = None) -> str:
    """The bar MovieBox draws down the left of the row you are on."""
    return glyph(*_SELECTION_BAR, plain=plain)


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------

#: kind -> (fancy badge, plain badge, css class), matching MovieBox's toasts.
NOTICES: dict[str, tuple[str, str, str]] = {
    "info": ("ℹ", "i", "notice-info"),
    "success": ("✔", "+", "notice-success"),
    "warning": ("⚠", "!", "notice-warning"),
    "error": ("✖", "x", "notice-error"),
}


def notice(kind: str, message: str, *, plain: bool | None = None) -> tuple[str, str]:
    """(text, css class) for the status line."""
    fancy, plain_mark, css = NOTICES.get(kind, NOTICES["info"])
    if plain is None:
        plain = plain_terminal()
    return f"{plain_mark if plain else fancy}  {message}", css


def version() -> str:
    try:
        from importlib.metadata import version as _version

        return _version("SpotiFLAC")
    except Exception:
        return ""


def subtitle() -> str:
    """The line under the wordmark: version, then what this is."""
    released = version()
    return f"v{released}  ·  {TAGLINE}" if released else TAGLINE


def supports_wordmark() -> bool:
    """Whether stdout can render the block letterform at all."""
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding

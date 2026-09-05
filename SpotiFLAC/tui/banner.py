"""banner.py — The wordmark at the top, and the key hints at the bottom.

MovieBox opens on a centred block-letter wordmark with a version line under
it, and closes every screen with a row of ``[key] action`` hints. Those two
bands are most of what makes it recognisable before you have read a word of
it, so they are widgets here rather than decoration sprinkled into the app.

Both resize themselves. The wordmark drops from six rows of letterform to
two, and then to a word, as the terminal narrows or shortens — a logo that
eats a quarter of a short screen is a logo in the way. The hint bar drops
hints from the right, keeping the ones you cannot work without.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Static

from .branding import TAGLINE as TAGLINE_ONLY
from .branding import hint_bar, hint_bar_markup, version, wordmark_for

#: Hints, most important first — the tail is what gets dropped when narrow.
DEFAULT_HINTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+R", "Run"),
    ("Ctrl+C", "Stop"),
    ("/", "Search"),
    ("Ctrl+Y", "Copy CLI"),
    ("Ctrl+L", "Log"),
    ("?", "Help"),
    ("t", "Theme"),
    ("q", "Quit"),
)


class Banner(Container):
    """The wordmark and the line under it, centred, full width."""

    def compose(self) -> ComposeResult:
        yield Static("", id="wordmark")
        yield Static("", id="wordmark-subtitle")

    def on_mount(self) -> None:
        self._redraw()

    def on_resize(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        # The screen's height, not the banner's: the question is how much of
        # the terminal a six-row logo would be taking, and the banner's own
        # height is the answer to that, not the input.
        available_height = self.screen.size.height if self.screen else 24
        art = wordmark_for(self.size.width or 80, available_height)
        rows = len(art.split("\n"))

        self.query_one("#wordmark", Static).update(art)
        self.query_one("#wordmark-subtitle", Static).update(
            self._subtitle_for(art, self.size.width or 80),
        )
        # +1 for the subtitle, +1 for the container's own top padding — leave
        # the second one out and the subtitle is the row that gets clipped.
        # Sized here rather than in CSS because the art is the only thing that
        # knows how tall it decided to be.
        self.styles.height = rows + 2
        self.set_class(rows == 1, "banner-plain")


    @staticmethod
    def _subtitle_for(art: str, width: int) -> str:
        """The version, hung off the wordmark's right edge.

        MovieBox tucks `v0.1.13` under the last letter rather than centring
        it, which is what stops the two lines reading as one centred block
        with a stray number in it. Reproduced by padding rather than by
        alignment, because the wordmark is centred and only its own width
        says where its right edge fell.
        """
        released = version()
        if not released:
            return TAGLINE_ONLY

        art_width = max(len(line) for line in art.split("\n"))
        if art_width <= 1 or art_width >= width:
            return f"v{released}  ·  {TAGLINE_ONLY}"

        left_margin = (width - art_width) // 2
        label = f"v{released}"
        pad = max(0, left_margin + art_width - len(label))
        return " " * pad + label


class HintBar(Static):
    """``[Ctrl+R] Run   [/] Search   …`` along the bottom."""

    def __init__(self, hints: tuple[tuple[str, str], ...] = DEFAULT_HINTS, **kwargs):
        super().__init__("", **kwargs)
        self._hints = hints

    def on_mount(self) -> None:
        self._redraw()

    def on_resize(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        width = self.size.width or 80
        hints = list(self._hints)
        # Measured on the plain form and rendered as markup: the colour codes
        # take no columns, so measuring the marked-up string would drop hints
        # that fit perfectly well.
        while hints and len(hint_bar(*hints)) > width - 2:
            hints.pop()
        self.update(hint_bar_markup(*hints))

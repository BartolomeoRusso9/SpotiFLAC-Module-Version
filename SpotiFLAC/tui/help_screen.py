"""help_screen.py — What the keys do, without leaving the screen.

The footer shows the handful of bindings there is room for; this is the rest,
plus the two things a first-time user actually needs told — that settings go
grey when something else has decided them, and that the Command panel is how
you graduate to the CLI.

Deliberately a modal rather than a panel: help you have to navigate *to* is
help you look for somewhere else instead.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

#: (key, what it does). Kept as data so the screen and any future cheat-sheet
#: cannot disagree about what is bound.
KEYS: tuple[tuple[str, str], ...] = (
    ("Ctrl+R", "Start the download"),
    ("Ctrl+C", "Stop the download, or quit when nothing is running"),
    ("Ctrl+L", "Show or hide the log pane"),
    ("/", "Jump to the search box"),
    ("Tab / Shift+Tab", "Move between controls"),
    ("j / k", "Same, without leaving the home row"),
    ("t", "Cycle the theme"),
    ("?", "This screen"),
    ("q", "Quit"),
)

_NOTES = """\
Settings that are greyed out are not broken — something else has already \
decided them. A bitrate needs a lossy conversion target; an artist separator \
needs more than one artist kept; track numbering and artist/album subfolders \
are alternatives, not both.

The Command panel shows the equivalent `spotiflac ...` invocation, updated as \
you type. Once a configuration is one you run often, copy it into a script \
and stop opening this.\
"""


class HelpScreen(ModalScreen[None]):
    """The key list, over the top of whatever was on screen."""

    BINDINGS = [
        Binding("escape,q,question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        rows = "\n".join(f"  {key:<18}{what}" for key, what in KEYS)
        with Center():
            with VerticalScroll(id="help-box"):
                yield Static("Keys", classes="help-heading")
                yield Static(rows, classes="help-keys")
                yield Static("Worth knowing", classes="help-heading")
                yield Static(_NOTES, classes="help-notes")
                yield Static("Esc or ? to close", classes="help-footer")

"""clipboard.py — Getting text out of the terminal UI.

Textual's `copy_to_clipboard` writes an OSC 52 escape and leaves the rest to
the terminal. That is the right route over ssh and in iTerm2, kitty, WezTerm
or tmux — and it does nothing at all in macOS Terminal, which ignores the
sequence without a word. Ctrl+Y and Ctrl+O both reported "copied" there and
put nothing on the clipboard.

So both routes are taken: OSC 52 always, and the system clipboard as well
when a copy command is at hand (pbcopy, wl-copy, xclip, xsel, clip). Not
over ssh, though: there the command would fill the clipboard of the machine
the app runs on, not the one in front of the user, and OSC 52 is the only
thing that reaches them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any


def _native_command() -> tuple[list[str], str] | None:
    """The system clipboard's copy command here, with the text encoding it
    reads, or None."""
    if sys.platform == "darwin":
        return (["pbcopy"], "utf-8") if shutil.which("pbcopy") else None
    if sys.platform.startswith("win"):
        # clip.exe reads UTF-16 when the input starts with a BOM, which
        # Python's "utf-16" codec writes; anything else it takes as the
        # console code page.
        return (["clip"], "utf-16") if shutil.which("clip") else None
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"], "utf-8"
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"], "utf-8"
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input"], "utf-8"
    return None


def _over_ssh() -> bool:
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def native_copy(text: str) -> bool:
    """Puts `text` on the system clipboard; True when that worked."""
    found = _native_command()
    if found is None:
        return False
    command, encoding = found
    try:
        subprocess.run(
            command,
            input=text.encode(encoding),
            check=True,
            timeout=5,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def copy_text(app: Any, text: str) -> bool:
    """Copies `text` both ways. True when the system clipboard has it for
    certain; False when only the OSC 52 sequence went out, which the
    terminal may or may not act on."""
    app.copy_to_clipboard(text)
    if _over_ssh():
        return False
    return native_copy(text)


#: What to add to a "copied" message when only OSC 52 carried it.
TERMINAL_ONLY_NOTE = (
    "sent to the terminal (OSC 52) — if nothing pastes, this terminal does not "
    "support it; iTerm2, kitty and WezTerm do, macOS Terminal does not"
)

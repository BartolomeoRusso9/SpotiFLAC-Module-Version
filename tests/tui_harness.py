"""One copy of the harness every TUI test file was carrying.

`drives_the_ui` was written out identically in eleven files: the project has
no pytest-asyncio, so an async test body has to be handed to `asyncio.run`,
and each file grew its own decorator to say so.

What is deliberately *not* here is `_settled`. It looks just as copyable, but
the number of `pilot.pause()` cycles is a per-panel fact — the extensions and
tracklist panels need 15, most need 12, the fixes file gets away with 6 — and
folding them into one default is how a panel that needs longer starts failing
somewhere else. Each file keeps its own, and anything shared would have to
take the count as an argument.
"""

from __future__ import annotations

import asyncio
import functools


def drives_the_ui(test):
    """Runs an async test body, the way the rest of this suite does."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper

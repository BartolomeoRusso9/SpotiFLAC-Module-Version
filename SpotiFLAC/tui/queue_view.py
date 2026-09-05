"""queue_view.py — The download queue, live, from the broadcaster's events.

Every dict this panel renders comes from `DownloadBroadcaster`, the same
channel the GUI has always consumed: `downloads` is the whole queue with a
status and a byte count per track, and the totals sit alongside it. Nothing
here parses console output, which is why the panel can be this small.

A track keeps its row when it finishes rather than disappearing from the
list. A queue that empties as it succeeds shows you least when the run is
going well, and leaves you unable to answer the question you actually have
afterwards — which of these did *not* work.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, ProgressBar, Static

#: Status → the glyph that leads the row. Deliberately monochrome-safe: a
#: terminal with no colour, or a user who cannot rely on it, still gets the
#: outcome from the shape alone.
_STATUS_MARK = {
    "queued": "·",
    "downloading": "⬇",
    "completed": "✓",
    "failed": "✗",
    "skipped": "⏭",
}

_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})


class TrackRow(Static):
    """One track: what it is, how far along, and how it ended."""

    def __init__(self, item: dict) -> None:
        super().__init__(classes="track-row")
        self._item = item
        self._bar: ProgressBar | None = None
        self._label: Label | None = None

    def compose(self) -> ComposeResult:
        self._label = Label(self._title_for(self._item), classes="track-title")
        # `total` is not known until the first chunk arrives, and a bar with
        # no total renders as indeterminate — which is exactly the honest
        # thing to show while the provider is still being asked.
        self._bar = ProgressBar(total=None, show_eta=False, classes="track-bar")
        yield Horizontal(self._label, self._bar)

    def on_mount(self) -> None:
        self.apply(self._item)

    @staticmethod
    def _title_for(item: dict) -> str:
        mark = _STATUS_MARK.get(str(item.get("status", "")), "·")
        title = str(item.get("track_name") or "unknown")
        artist = str(item.get("artist_name") or "")
        line = f"{mark} {title}"
        if artist:
            line += f" — {artist}"
        if item.get("status") == "failed" and item.get("error_message"):
            line += f"  ({str(item['error_message'])[:40]})"
        return line

    def apply(self, item: dict) -> None:
        """Re-renders the row from a fresh copy of its queue entry."""
        self._item = item
        status = str(item.get("status", ""))

        if self._label is not None:
            self._label.update(self._title_for(item))
        self.set_class(status == "failed", "failed")
        self.set_class(status == "completed", "completed")
        self.set_class(status == "downloading", "active")

        if self._bar is None:
            return

        total = float(item.get("total_size") or 0.0)
        progress = float(item.get("progress") or 0.0)
        if status in _TERMINAL_STATUSES:
            # Completed means the bar is full; skipped and failed have no
            # meaningful fraction, so they get a full bar too and say what
            # happened in the label rather than pretending to a percentage.
            self._bar.update(total=1.0, progress=1.0)
        elif total > 0:
            self._bar.update(total=total, progress=min(progress, total))
        else:
            self._bar.update(total=None)


class QueuePanel(VerticalScroll):
    """The whole queue: a master bar over one row per track."""

    BORDER_TITLE = "Queue"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: dict[str, TrackRow] = {}
        self._master: ProgressBar | None = None
        self._summary: Label | None = None
        self._empty: Label | None = None

    def compose(self) -> ComposeResult:
        self._master = ProgressBar(total=None, show_eta=False, id="master-bar")
        self._summary = Label("Nothing running", id="queue-summary")
        self._empty = Label(
            "The queue fills up once a download starts.",
            id="queue-empty",
        )
        yield Vertical(self._summary, self._master, id="queue-header")
        yield self._empty

    def reset(self) -> None:
        """Clears the queue for a new run."""
        for row in self._rows.values():
            row.remove()
        self._rows.clear()
        if self._empty is not None:
            self._empty.display = True
        if self._master is not None:
            self._master.update(total=None, progress=0)
        if self._summary is not None:
            self._summary.update("Nothing running")

    def apply_stats(self, stats: dict) -> None:
        """Folds one broadcaster event into the panel.

        Rows are added as tracks appear and updated in place after that; the
        queue is append-only within a run, so nothing has to be removed and
        the list never jumps around under the cursor.
        """
        items = stats.get("downloads") or stats.get("queue") or []
        if items and self._empty is not None:
            self._empty.display = False

        for item in items:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            row = self._rows.get(item_id)
            if row is None:
                row = TrackRow(item)
                self._rows[item_id] = row
                self.mount(row)
            else:
                row.apply(item)

        self._update_totals(stats, len(items))

    def _update_totals(self, stats: dict, total_items: int) -> None:
        done = (
            int(stats.get("completed", 0))
            + int(stats.get("failed", 0))
            + int(stats.get("skipped", 0))
        )
        if self._master is not None:
            self._master.update(total=total_items or None, progress=done)

        if self._summary is None:
            return
        from .runner import make_status_line

        self._summary.update(make_status_line(stats) or "Nothing running")

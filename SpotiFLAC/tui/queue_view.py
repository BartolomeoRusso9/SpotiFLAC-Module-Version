"""queue_view.py — The download queue, live, from the broadcaster's events.

Every dict this panel renders comes from `DownloadBroadcaster`, the same
channel the GUI has always consumed: `downloads` is the whole queue with a
status and a byte count per track, and the totals sit alongside it. Nothing
here parses console output, which is why the panel can be this small.

A track keeps its row when it finishes rather than disappearing from the
list. A queue that empties as it succeeds shows you least when the run is
going well, and leaves you unable to answer the question you actually have
afterwards — which of these did *not* work.

One column, and only text in it. The panel carried the track's cover art
for a while, first above the list and then beside it, and both cost the
queue the thing it is for: above, every row of artwork was a row of queue
given up; beside, the rows lost half the width and their titles turned into
ellipses. The screen is two columns now — this panel and the log next to it
(`app.py`) — and the queue gets all of one.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, ProgressBar

from .branding import status_badge

_BADGE_CLASSES = (
    "badge-gold",
    "badge-sapphire",
    "badge-teal",
    "badge-lavender",
    "badge-success",
    "badge-error",
    "badge-muted",
)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})


class TrackRow(Horizontal):
    """One track: what it is, how far along, and how it ended.

    A container, not a `Static`. A Static sizes itself from its own
    renderable, and this one's is empty — so with `height: auto` every row
    came out zero rows tall and the queue looked empty while it was in fact
    full.
    """

    def __init__(self, item: dict) -> None:
        super().__init__(classes="track-row")
        self._item = item
        self._bar: ProgressBar | None = None
        self._label: Label | None = None
        self._badge: Label | None = None

    def compose(self) -> ComposeResult:
        # The badge is MovieBox's resolution chip, doing the same job: the
        # outcome, readable at a glance from colour and shape together, so
        # colour alone is never carrying it.
        self._badge = Label("", classes="track-badge", markup=False)
        self._label = Label(self._title_for(self._item), classes="track-title")
        # `total` is not known until the first chunk arrives, and a bar with
        # no total renders as indeterminate — which is exactly the honest
        # thing to show while the provider is still being asked.
        self._bar = ProgressBar(total=None, show_eta=False, classes="track-bar")
        yield self._badge
        yield self._label
        yield self._bar

    def on_mount(self) -> None:
        self.apply(self._item)

    @staticmethod
    def _title_for(item: dict) -> str:
        title = str(item.get("track_name") or "unknown")
        artist = str(item.get("artist_name") or "")
        line = title if not artist else f"{title} — {artist}"
        if item.get("status") == "failed" and item.get("error_message"):
            line += f"  ({str(item['error_message'])[:36]})"
        return line

    def apply(self, item: dict) -> None:
        """Re-renders the row from a fresh copy of its queue entry."""
        self._item = item
        status = str(item.get("status", ""))

        if self._label is not None:
            self._label.update(self._title_for(item))
        if self._badge is not None:
            text, css = status_badge(status)
            self._badge.update(text)
            for candidate in _BADGE_CLASSES:
                self._badge.set_class(candidate == css, candidate)
        self.set_class(status == "failed", "failed")
        self.set_class(status == "completed", "completed")
        self.set_class(status == "downloading", "active")

        if self._bar is None:
            return

        total = float(item.get("total_size") or 0.0)
        progress = float(item.get("progress") or 0.0)
        if status == "completed":
            self._bar.update(total=1.0, progress=1.0)
        elif status in _TERMINAL_STATUSES or status == "queued":
            # Empty, not full and not pulsing. A failed track with a full bar
            # reads as a success, and a queued one with the indeterminate
            # animation reads as busy — both say the opposite of the truth.
            # The badge carries the outcome; the bar only carries progress.
            self._bar.update(total=1.0, progress=0.0)
        elif total > 0:
            self._bar.update(total=total, progress=min(progress, total))
        else:
            self._bar.update(total=None)


class QueuePanel(VerticalScroll):
    """The whole queue: the totals, then one row per track."""

    BORDER_TITLE = "Queue"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: dict[str, TrackRow] = {}
        self._master: ProgressBar | None = None
        self._summary: Label | None = None
        self._now: Label | None = None
        self._empty: Label | None = None

    def compose(self) -> ComposeResult:
        # `total=1.0` rather than `None`. A bar with no total renders as the
        # indeterminate animation, so an idle queue sat there pulsing under
        # the words "Nothing running" — the same lie `TrackRow.apply()`
        # already refuses to tell for a queued row.
        self._master = ProgressBar(total=1.0, show_eta=False, id="master-bar")
        self._summary = Label("Nothing running", id="queue-summary")
        # Which track the totals are about. Without it the master bar is a
        # percentage of nothing you can name.
        self._now = Label("", id="queue-now", markup=False)
        self._empty = Label(
            "The queue fills up once a download starts.",
            id="queue-empty",
        )
        yield Vertical(self._summary, self._now, self._master, id="queue-header")
        yield self._empty

    def reset(self) -> None:
        """Clears the queue for a new run."""
        for row in self._rows.values():
            row.remove()
        self._rows.clear()
        if self._empty is not None:
            self._empty.display = True
        if self._master is not None:
            self._master.update(total=1.0, progress=0)
        if self._summary is not None:
            self._summary.update("Nothing running")
        if self._now is not None:
            self._caption("")

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

        self._show_current(items)
        self._update_totals(stats, len(items))

    @staticmethod
    def _current_item(items: list[dict]) -> dict | None:
        """Which track the header is about.

        The one being fetched, if there is one. Otherwise the most recent to
        have finished — at the end of a run that leaves the last track on
        screen rather than blanking the header the moment the work is done.
        And failing both, the first in the queue, so the artwork is up while
        the providers are still being asked rather than appearing a beat
        into the download.
        """
        for item in items:
            if str(item.get("status", "")) == "downloading":
                return item
        finished = [i for i in items if float(i.get("end_time") or 0.0) > 0]
        if finished:
            return max(finished, key=lambda i: float(i.get("end_time") or 0.0))
        return items[0] if items else None

    def _caption(self, text: str) -> None:
        """The line under the totals, gone entirely when it says nothing.

        An empty caption is still a row of the layout, and a blank line
        holding the queue open above an idle panel looks like something
        failed to load.
        """
        if self._now is None:
            return
        self._now.update(text)
        self._now.display = bool(text)

    def _show_current(self, items: list[dict]) -> None:
        item = self._current_item(items)
        if item is None:
            return
        self._caption(TrackRow._title_for(item))

    def _update_totals(self, stats: dict, total_items: int) -> None:
        done = (
            int(stats.get("completed", 0))
            + int(stats.get("failed", 0))
            + int(stats.get("skipped", 0))
        )
        if self._master is not None:
            self._master.update(total=total_items or 1.0, progress=done)

        if self._summary is None:
            return
        from .runner import make_status_line

        self._summary.update(make_status_line(stats) or "Nothing running")

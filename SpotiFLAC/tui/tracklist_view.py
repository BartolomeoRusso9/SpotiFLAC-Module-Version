"""tracklist_view.py — The tracks behind a link, and which of them you want.

The plan's §10 asks for this: a table of the tracks with multi-selection on
space. It is the one thing the GUI could do that the command line cannot,
because the CLI takes a link and downloads what is behind it, all of it.

The mechanics come from `core/tracklist.py`, which resolves a link into
tracks and then answers the only question that matters at download time:
what do I hand the downloader? Everything selected gives back the collection
URL — one resolution, the album's own ordering and numbering intact — and a
strict subset gives back a list of per-track links. `_run_download_async`
takes either.

Resolving is deliberate rather than automatic. It is a network round trip
against a link you may still be editing, and doing it on every keystroke in
the URL field would be both slow and rude.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, DataTable, Label

from ..core.tracklist import Tracklist, resolve_tracklist, track_url
from .branding import glyph

_COLUMNS = ("", "#", "Title", "Artist", "Album")

#: (selected, unselected) — the box in the first column.
_TICK = ("✔", "x")
_EMPTY = ("·", " ")


class TracklistPanel(VerticalScroll):
    """A table of the tracks behind the configured link."""

    BORDER_TITLE = "Tracks"

    BINDINGS = [
        Binding("space", "toggle_row", "Toggle", show=False),
        Binding("a", "select_all", "All", show=False),
        Binding("n", "select_none", "None", show=False),
        Binding("i", "invert", "Invert", show=False),
    ]

    class SelectionChanged(Message):
        """The selection changed; the app may want to say so."""

        def __init__(self, selected: int, total: int) -> None:
            super().__init__()
            self.selected = selected
            self.total = total

    def __init__(self, get_url, **kwargs) -> None:
        super().__init__(**kwargs)
        # A callable, not a string: the URL is edited on another panel and
        # this one should read it at the moment somebody asks to load.
        self._get_url = get_url
        self.tracklist = Tracklist()
        self.selected: set[int] = set()
        self._loading = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Label(
            "The tracks behind the link. Space picks one, a picks all, "
            "n picks none, i inverts.",
            classes="panel-intro",
        )
        yield Horizontal(
            Button("Load tracks", id="tracks-load", variant="primary"),
            Button("All", id="tracks-all"),
            Button("None", id="tracks-none"),
            classes="setting-row",
        )
        yield Label("", id="tracks-status")
        table: DataTable = DataTable(id="tracks-table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#tracks-table", DataTable)
        for column in _COLUMNS:
            table.add_column(column, key=column)
        self._say("Press Load tracks to see what is behind the link.")

    def _say(self, message: str) -> None:
        self.query_one("#tracks-status", Label).update(message)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tracks-load":
            self.load()
        elif event.button.id == "tracks-all":
            self.action_select_all()
        elif event.button.id == "tracks-none":
            self.action_select_none()

    def load(self) -> None:
        if self._loading:
            return
        url = (self._get_url() or "").strip()
        if not url:
            self._say("Set a URL on the Download panel first.")
            return
        self._loading = True
        self._say(f"Reading {url}…")
        self.run_worker(self._load(url), exclusive=True)

    async def _load(self, url: str) -> None:
        try:
            tracklist = await resolve_tracklist(url)
        except Exception as exc:
            self._say(f"Could not read it — {type(exc).__name__}: {exc}")
            self._loading = False
            return

        self.tracklist = tracklist
        # Everything, to begin with: the panel exists to let you take tracks
        # away, and opening on an empty selection would make it a chore you
        # have to complete rather than one you can skip.
        self.selected = set(range(len(tracklist)))
        self._redraw()

        if not tracklist:
            self._say("No tracks at that link.")
        else:
            name = tracklist.name or "the link"
            self._say(f"{len(tracklist)} track(s) in {name}. All selected.")
        self._loading = False
        self._announce()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _mark(self, index: int) -> str:
        chosen = index in self.selected
        return glyph(*_TICK) if chosen else glyph(*_EMPTY)

    def _redraw(self) -> None:
        table = self.query_one("#tracks-table", DataTable)
        table.clear()
        for index, track in enumerate(self.tracklist.tracks):
            linkable = bool(track_url(track, self.tracklist.source_url))
            table.add_row(
                self._mark(index),
                str(index + 1),
                str(getattr(track, "title", "") or "unknown"),
                str(getattr(track, "artists", "") or ""),
                # A track with no link of its own cannot be fetched alone, so
                # say that here rather than at download time.
                str(getattr(track, "album", "") or "") if linkable else "no link",
                key=str(index),
            )

    def _refresh_marks(self) -> None:
        table = self.query_one("#tracks-table", DataTable)
        for index in range(len(self.tracklist)):
            try:
                table.update_cell(str(index), "", self._mark(index))
            except Exception:
                continue

    def _announce(self) -> None:
        self.post_message(
            self.SelectionChanged(len(self.selected), len(self.tracklist)),
        )

    def action_toggle_row(self) -> None:
        table = self.query_one("#tracks-table", DataTable)
        index = table.cursor_row
        if index is None or not (0 <= index < len(self.tracklist)):
            return
        self.selected.symmetric_difference_update({index})
        self._refresh_marks()
        self._say_count()
        self._announce()

    def action_select_all(self) -> None:
        self.selected = set(range(len(self.tracklist)))
        self._refresh_marks()
        self._say_count()
        self._announce()

    def action_select_none(self) -> None:
        self.selected.clear()
        self._refresh_marks()
        self._say_count()
        self._announce()

    def action_invert(self) -> None:
        self.selected.symmetric_difference_update(range(len(self.tracklist)))
        self._refresh_marks()
        self._say_count()
        self._announce()

    def _say_count(self) -> None:
        total = len(self.tracklist)
        if not total:
            return
        chosen = len(self.selected)
        if chosen == total:
            self._say(f"All {total} selected.")
        elif chosen == 0:
            self._say(f"None of {total} selected — nothing would download.")
        else:
            self._say(f"{chosen} of {total} selected.")

    # ------------------------------------------------------------------
    # What the run should fetch
    # ------------------------------------------------------------------

    @property
    def has_selection(self) -> bool:
        """Whether this panel has an opinion about what to download.

        An unloaded panel has none, and the run should use the URL as it
        always did rather than an empty list.
        """
        return bool(self.tracklist)

    @property
    def is_whole_collection(self) -> bool:
        return len(self.selected) == len(self.tracklist)

    def selected_indices(self) -> list[int]:
        return sorted(self.selected)

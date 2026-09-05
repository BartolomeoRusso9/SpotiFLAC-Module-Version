"""search_view.py — Finding something to download without leaving the UI.

The catalogue search the desktop GUI has always had, over the same code:
`api_mixins.search.search_metadata_async()`, which is the async half of what
used to live twice inside `app.py`. Results are the four sections Spotify
returns — tracks, albums, artists, playlists — and picking any of them does
one thing: puts its URL in the Download panel's URL field.

That is the whole design. A search panel that also downloaded would be a
second, subtly different copy of the Download panel's rules; instead this one
answers "which link did you mean" and hands the answer over.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import DataTable, Input, Label

#: Which sections to show, and in the order a search is usually read.
SECTIONS: tuple[str, ...] = ("tracks", "albums", "artists", "playlists")

_COLUMNS = ("Kind", "Name", "By", "Detail")
_PER_SECTION = 20


def _row_for(kind: str, item: dict) -> tuple[str, str, str, str]:
    """One result as the four columns, whichever section it came from."""
    name = str(item.get("name") or item.get("title") or "")
    if kind == "tracks":
        return ("track", name, str(item.get("artists") or ""), str(item.get("album") or ""))
    if kind == "albums":
        return ("album", name, str(item.get("artists") or ""), str(item.get("release_date") or ""))
    if kind == "artists":
        return ("artist", name, "", "")
    return ("playlist", name, str(item.get("owner") or ""), "")


class SearchPanel(VerticalScroll):
    """A query box and a table of what came back."""

    BORDER_TITLE = "Search"

    class UrlChosen(Message):
        """A result was picked; its URL should become the download target."""

        def __init__(self, url: str, label: str) -> None:
            super().__init__()
            self.url = url
            self.label = label

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Row key -> (url, label). The table holds text; this holds what the
        # text stands for.
        self._urls: dict[str, tuple[str, str]] = {}
        self._searching = False

    def compose(self) -> ComposeResult:
        yield Label(
            "Search the catalogue. Pick a result and it becomes the URL to "
            "download.",
            classes="panel-intro",
        )
        yield Input(placeholder="artist, album or track…", id="search-query")
        yield Label("", id="search-status")
        table: DataTable = DataTable(id="search-results", zebra_stripes=True)
        table.cursor_type = "row"
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#search-results", DataTable)
        for column in _COLUMNS:
            table.add_column(column, key=column)

    def focus_query(self) -> None:
        self.query_one("#search-query", Input).focus()

    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-query":
            self.search(event.value)

    def search(self, query: str) -> None:
        query = (query or "").strip()
        if not query:
            self.query_one("#search-status", Label).update("Type something to find.")
            return
        if self._searching:
            return
        self._searching = True
        self.query_one("#search-status", Label).update(f"Searching for “{query}”…")
        self.run_worker(self._search(query), exclusive=True)

    async def _search(self, query: str) -> None:
        try:
            from ..api_mixins.search import search_metadata_async

            results = await search_metadata_async(query)
        except Exception as exc:
            self.query_one("#search-status", Label).update(f"Search failed — {exc}")
            self._searching = False
            return

        table = self.query_one("#search-results", DataTable)
        table.clear()
        self._urls.clear()

        found = 0
        for kind in SECTIONS:
            for item in (results.get(kind) or [])[:_PER_SECTION]:
                url = str(item.get("external_url") or item.get("external_urls") or "")
                if not url:
                    # Without a link there is nothing to hand to the
                    # downloader, so the row would only be decorative.
                    continue
                row = _row_for(kind, item)
                key = f"{kind}:{item.get('id') or found}"
                table.add_row(*row, key=key)
                self._urls[key] = (url, row[1])
                found += 1

        self.query_one("#search-status", Label).update(
            f"{found} result(s) for “{query}”." if found else f"Nothing for “{query}”.",
        )
        self._searching = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        chosen = self._urls.get(str(event.row_key.value))
        if chosen is None:
            return
        url, label = chosen
        self.post_message(self.UrlChosen(url, label))

"""csv_picker_screen.py — Choosing a track list without typing its path.

The path to a CSV is the one thing in the whole configuration that is
genuinely awkward to type: it is long, it was exported minutes ago into
whichever folder the browser uses, and getting it wrong is only discovered
later. So the picker offers what is lying around — `core.csv_picker` does the
scanning — and reads the file before accepting it.

That last part is the point of the preview. A CSV that parses is not
necessarily the right CSV, and a row count plus the first few titles is what
tells someone they picked last month's export. Catching it here costs a
second; catching it after the run has started costs the run.
"""

from __future__ import annotations

import datetime as _datetime
import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core.csv_picker import (
    clean_path_input,
    csv_scan_dirs,
    format_size,
    read_csv_document,
    scan_csv_files,
    short_dir,
)

_PREVIEW_ROWS = 3


class CsvPickerScreen(ModalScreen[str | None]):
    """Returns the chosen path, or None when dismissed.

    A modal because picking a file is a detour: you come back to the same
    form, with one field filled in.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, seed_dir: str = "") -> None:
        super().__init__()
        self._seed_dir = seed_dir
        self._candidates: list[tuple[str, float, float]] = []

    def compose(self) -> ComposeResult:
        with Center():
            with VerticalScroll(id="csv-box"):
                yield Static("Pick a track list", classes="help-heading")
                yield Label(
                    "Files found in the folders a track list usually lands in.",
                    classes="panel-intro",
                )
                yield OptionList(id="csv-candidates")
                yield Label("Or type a path", classes="help-heading")
                yield Input(placeholder="~/Downloads/wishlist.csv", id="csv-path")
                yield Static("", id="csv-preview")
                yield Horizontal(
                    Button("Use this file", id="csv-accept", variant="primary"),
                    Button("Cancel", id="csv-cancel"),
                    classes="setting-row",
                )

    def on_mount(self) -> None:
        self.run_worker(self._scan(), exclusive=False)

    # ------------------------------------------------------------------

    async def _scan(self) -> None:
        last_folder = ""
        try:
            from ..core.session_memory import get_last_folder_async

            last_folder = await get_last_folder_async() or ""
        except Exception:
            last_folder = ""

        self._candidates = scan_csv_files(
            csv_scan_dirs(self._seed_dir, last_folder),
        )

        listing = self.query_one("#csv-candidates", OptionList)
        listing.clear_options()
        for path, mtime, size in self._candidates:
            when = _datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            listing.add_option(
                Option(
                    f"{os.path.basename(path)}\n"
                    f"  {short_dir(os.path.dirname(path))} · "
                    f"{format_size(size)} · {when}",
                    id=path,
                ),
            )
        if not listing.option_count:
            listing.add_option(Option("Nothing found — type a path below", id=""))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = event.option.id
        if not chosen:
            return
        self.query_one("#csv-path", Input).value = chosen
        self.run_worker(self._preview(chosen), exclusive=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.run_worker(self._accept(event.value), exclusive=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "csv-cancel":
            self.dismiss(None)
            return
        self.run_worker(
            self._accept(self.query_one("#csv-path", Input).value),
            exclusive=True,
        )

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    # ------------------------------------------------------------------

    async def _preview(self, raw: str) -> str:
        """Reads the file and describes it. Returns "" when it is unusable."""
        preview = self.query_one("#csv-preview", Static)
        path = clean_path_input(raw)
        if not path:
            preview.update("")
            return ""

        if not os.path.isfile(path):
            preview.update(f"No file at {path}")
            return ""

        document, error = await read_csv_document(path)
        if document is None:
            preview.update(f"Could not read it — {error}")
            return ""
        if not document.rows:
            preview.update("It parsed, but there are no tracks in it.")
            return ""

        columns = ", ".join(document.columns) if document.columns else "positional"
        delimiter = {"\t": "tab"}.get(document.delimiter, document.delimiter)
        lines = [
            f"{len(document.rows)} tracks · delimiter {delimiter} · fields: {columns}",
        ]
        lines += [f"  · {row.label[:60]}" for row in document.rows[:_PREVIEW_ROWS]]
        if len(document.rows) > _PREVIEW_ROWS:
            lines.append(f"  … {len(document.rows) - _PREVIEW_ROWS} more")
        preview.update("\n".join(lines))
        return path

    async def _accept(self, raw: str) -> None:
        path = await self._preview(raw)
        if path:
            self.dismiss(path)

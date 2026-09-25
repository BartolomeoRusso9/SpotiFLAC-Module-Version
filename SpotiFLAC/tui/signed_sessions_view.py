"""signed_sessions_view.py — Which signed sessions are live, and until when?

A download that suddenly opens a browser to solve a challenge is a session
that expired; one that fails with "verification paused" is a gateway that
said no. Both used to be visible only as a line in the log. This panel reads
the stores in `~/.spotiflac/signed_sessions/` through
`core.signed_session_status` — the same call `--signed-sessions` makes — and
shows one row per session with its countdown.

Reading is local and cheap, so it loads on mount and re-reads every
`_REFRESH_S` seconds while mounted; no network traffic either way.
"""

from __future__ import annotations

from typing import Any, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Label

_COLUMNS = ("Session", "State", "Expires", "Detail")
_REFRESH_S = 30


class SignedSessionsPanel(VerticalScroll):
    """One row per stored signed session, with clear and prune."""

    BORDER_TITLE = "Signed sessions"

    def compose(self) -> ComposeResult:
        yield Label(
            "Verified sessions extensions use for their gateway. An expired one "
            "is renewed with a new verification the next time it is needed.",
            classes="panel-intro",
        )
        with Horizontal(classes="setting-row"):
            yield Button("Refresh", id="signed-refresh", variant="primary")
            yield Button("Clear selected", id="signed-clear")
            yield Button("Remove orphaned", id="signed-prune")
        yield Label("", id="signed-status")
        table: DataTable = DataTable(id="signed-table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#signed-table", DataTable)
        for column in _COLUMNS:
            table.add_column(column, key=column)
        self.reload()
        self.set_interval(_REFRESH_S, self.reload)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "signed-refresh":
            self.reload()
        elif event.button.id == "signed-clear":
            self.clear_selected()
        elif event.button.id == "signed-prune":
            self.prune()

    def _status(self, text: str) -> None:
        self.query_one("#signed-status", Label).update(text)

    def reload(self) -> None:
        from ..core import signed_session_status as sss

        try:
            rows = sss.list_signed_sessions()
        except Exception as exc:
            self._status(f"Could not read the sessions — {exc}")
            return

        table = self.query_one("#signed-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for row in rows:
            label = row["label"] + (f" v{row['version']}" if row.get("version") else "")
            table.add_row(
                label,
                sss.STATE_LABELS.get(row["state"], row["state"]),
                sss.describe_expiry(row),
                sss.describe_row_detail(row)[:60],
                key=row["key"],
            )
        if rows:
            table.move_cursor(row=min(cursor, len(rows) - 1))

        if not rows:
            self._status("No signed sessions stored yet.")
            return
        active = sum(1 for r in rows if r["state"] in (sss.ACTIVE, sss.REFRESH_DUE))
        orphaned = sum(1 for r in rows if r["state"] == sss.ORPHANED)
        text = f"{active} of {len(rows)} active."
        if orphaned:
            text += f" {orphaned} orphaned — Remove orphaned deletes them."
        self._status(text)

    def _selected_key(self) -> str | None:
        table = self.query_one("#signed-table", DataTable)
        if not table.row_count:
            return None
        try:
            row_key, _ = cast(Any, table).coordinate_to_cell_key((table.cursor_row, 0))
        except Exception:
            return None
        return str(row_key.value) if row_key.value is not None else None

    def clear_selected(self) -> None:
        from ..core.signed_session_status import clear_signed_session

        key = self._selected_key()
        if not key:
            self._status("Select a session first.")
            return
        try:
            cleared = clear_signed_session(key)
        except Exception as exc:
            self._status(f"Could not clear {key} — {exc}")
            return
        self.reload()
        if cleared:
            self.app.notify(f"Cleared {key}. The next request verifies again.")

    def prune(self) -> None:
        from ..core.signed_session_status import prune_orphaned_sessions

        try:
            removed = prune_orphaned_sessions()
        except Exception as exc:
            self._status(f"Could not remove orphaned sessions — {exc}")
            return
        self.reload()
        self.app.notify(
            f"Removed {len(removed)} orphaned session(s)."
            if removed
            else "No orphaned sessions to remove."
        )

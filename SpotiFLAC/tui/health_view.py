"""health_view.py — Are the lyrics providers reachable right now?

The wizard ran this before its first question, which was the right instinct
in the wrong place: it made every run wait on the network to tell you
something you only need when lyrics come back empty. Here it is a panel you
open when that happens, and a button you press when you want it re-checked.

`core.health_check.run_health_check()` does the probing and returns a
`HealthResult` per provider; this renders it and nothing more.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Label

_COLUMNS = ("Provider", "Status", "Latency", "Detail")


class HealthPanel(VerticalScroll):
    """One row per lyrics provider, with a re-check button."""

    BORDER_TITLE = "Health"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._checking = False

    def compose(self) -> ComposeResult:
        yield Label(
            "Lyrics providers, probed directly. A provider that is down here "
            "is why its lyrics came back empty.",
            classes="panel-intro",
        )
        yield Button("Check now", id="health-check", variant="primary")
        yield Label("", id="health-status")
        table: DataTable = DataTable(id="health-table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#health-table", DataTable)
        for column in _COLUMNS:
            table.add_column(column, key=column)
        # Not probed on mount: opening a panel should not start network
        # traffic the user did not ask for, and the button is right there.
        self.query_one("#health-status", Label).update("Not checked yet.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "health-check":
            self.check()

    def check(self) -> None:
        if self._checking:
            return
        self._checking = True
        self.query_one("#health-status", Label).update("Checking…")
        self.run_worker(self._check(), exclusive=True)

    async def _check(self) -> None:
        try:
            from ..core.health_check import run_health_check

            results = await run_health_check()
        except Exception as exc:
            self.query_one("#health-status", Label).update(
                f"Could not run the check — {exc}",
            )
            self._checking = False
            return

        table = self.query_one("#health-table", DataTable)
        table.clear()
        for result in sorted(results, key=lambda r: r.provider):
            table.add_row(
                result.provider,
                "✓ up" if result.ok else "✗ down",
                f"{result.latency:.2f}s" if result.latency else "—",
                (result.detail or "")[:48],
            )

        reachable = sum(1 for result in results if result.ok)
        total = len(results)
        self.query_one("#health-status", Label).update(
            f"{reachable} of {total} reachable."
            if reachable == total
            else f"{reachable} of {total} reachable — the rest will fall back.",
        )
        self._checking = False

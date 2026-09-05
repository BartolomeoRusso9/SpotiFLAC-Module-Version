"""extensions_view.py — Where providers come from.

An extension registry is a link to a list of installable providers. Nothing
downloads until at least one is configured, which makes this the panel a
fresh install visits first and, once it works, never again.

`extensions/registry_config` is the same module the wizard's registry menu
and the GUI's Settings → Extensions tab read, so a link added here shows up
in both. The three sources it distinguishes matter: a link exported in the
terminal or written into a `.env` file cannot be edited from a UI — it comes
back the moment the process restarts — and saying so is more useful than
offering a delete that will not stick.

Editing the list installs from it straight away, rather than at the next
launch, because the reason to add a registry is always the provider you
wanted a minute ago.
"""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label

#: How each source reads to someone who did not set it up.
SOURCE_LABELS = {
    "environment": "terminal export",
    "env_file": ".env file",
    "custom": "added here",
}

_COLUMNS = ("Registry", "Source", "State")


class ExtensionsPanel(VerticalScroll):
    """The configured registry links, with add and remove."""

    BORDER_TITLE = "Extensions"

    def __init__(self, min_trust_tier: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._min_trust_tier = min_trust_tier
        self._urls: list[str] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Label(
            "Registry links. Providers install from these; without one "
            "there is nothing to download.",
            classes="panel-intro",
        )
        table: DataTable = DataTable(id="registry-table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Horizontal(
            Input(placeholder="https://…/registry.json", id="registry-url"),
            Button("Add", id="registry-add", variant="primary"),
            Button("Remove selected", id="registry-remove", variant="warning"),
            classes="setting-row",
        )
        yield Label("", id="registry-status")

    def on_mount(self) -> None:
        table = self.query_one("#registry-table", DataTable)
        for column in _COLUMNS:
            table.add_column(column, key=column)
        self.run_worker(self.reload(), exclusive=False)

    # ------------------------------------------------------------------

    def _say(self, message: str) -> None:
        self.query_one("#registry-status", Label).update(message)

    async def reload(self) -> None:
        try:
            from ..extensions import registry_config

            registries = await asyncio.to_thread(registry_config.list_registries)
        except Exception as exc:
            self._say(f"Could not read the registry list — {exc}")
            return

        table = self.query_one("#registry-table", DataTable)
        table.clear()
        self._urls = []
        for entry in registries:
            sources = ", ".join(
                SOURCE_LABELS.get(source, source) for source in entry.get("sources", [])
            )
            table.add_row(
                str(entry.get("url", "")),
                sources,
                "enabled" if entry.get("enabled") else "removed",
            )
            self._urls.append(str(entry.get("url", "")))

        if not self._urls:
            self._say("No registry links configured.")
        else:
            self._say(f"{len(self._urls)} link(s).")

    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._busy:
            return
        if event.button.id == "registry-add":
            url = self.query_one("#registry-url", Input).value.strip()
            if not url:
                self._say("Paste a registry link first.")
                return
            self.run_worker(self._add(url), exclusive=True)
        elif event.button.id == "registry-remove":
            url = self._selected_url()
            if not url:
                self._say("Select a link in the table first.")
                return
            self.run_worker(self._remove(url), exclusive=True)

    def _selected_url(self) -> str:
        table = self.query_one("#registry-table", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._urls)):
            return ""
        return self._urls[row]

    async def _add(self, url: str) -> None:
        self._busy = True
        self._say(f"Adding {url}…")
        try:
            from ..extensions import registry_config

            await asyncio.to_thread(registry_config.add_registry, url)
        except Exception as exc:
            self._say(f"Could not add it — {exc}")
            self._busy = False
            return

        self.query_one("#registry-url", Input).value = ""
        await self.reload()
        await self._sync()
        self._busy = False

    async def _remove(self, url: str) -> None:
        self._busy = True
        self._say(f"Removing {url}…")
        try:
            from ..extensions import registry_config

            await asyncio.to_thread(registry_config.remove_registry, url)
        except Exception as exc:
            self._say(f"Could not remove it — {exc}")
            self._busy = False
            return

        await self.reload()
        self._busy = False

    async def _sync(self) -> None:
        """Installs from the new list now, not at the next launch."""
        self._say("Installing providers from the registries…")
        try:
            from ..extensions.manager import ExtensionManager

            await asyncio.to_thread(
                ExtensionManager,
                auto_install_downloads=True,
                min_trust_tier=self._min_trust_tier,
            )
        except Exception as exc:
            self._say(f"Added, but installing from it failed — {exc}")
            return
        self._say("Added. Providers installed from the registries.")

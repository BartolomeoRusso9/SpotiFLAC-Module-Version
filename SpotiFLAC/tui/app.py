"""app.py — `spotiflac --tui`.

The guided mode, as one screen instead of fifteen questions. A sidebar picks
what the main panel shows; the panels themselves are small, because each one
sits on top of something the project already had:

* **Download** edits a `ConfigState`, which produces the same `cfg` dict the
  wizard produced (`config_view.py`, `config_state.py`).
* **Queue** renders `DownloadBroadcaster` events, the channel the GUI has
  consumed all along (`queue_view.py`).
* **Command** is the equivalent `spotiflac …` invocation, rebuilt on every
  keystroke — the wizard printed this once at the end, and the point of it
  was always to teach the CLI (`core/cli_preview.py`).

Nothing here imports `SpotiFLAC.app`: that module imports pywebview at module
level and its API object is synchronous and thread-based. The TUI talks to
`core/*` and `extensions/*` directly.

The log pane at the bottom is where the run's console output goes — not
because a UI needs a log, but because during a download there is nowhere else
for it to be. `core.output_sink` makes that possible; without it every one of
those lines would land on the terminal underneath and tear the layout.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    ContentSwitcher,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    Label,
    RichLog,
    Static,
)

from .config_state import ConfigState
from .config_view import ConfigPanel
from .extensions_view import ExtensionsPanel
from .health_view import HealthPanel
from .help_screen import HelpScreen
from .queue_view import QueuePanel
from .search_view import SearchPanel
from .session_view import SessionPanel
from .runner import FAILED, FINISHED, OUTPUT, STATS, DownloadRunner

#: Sidebar entries: (id, label), in the order the work is usually done.
MODES: tuple[tuple[str, str], ...] = (
    ("download", "Download"),
    ("search", "Search"),
    ("queue", "Queue"),
    ("session", "Session"),
    ("extensions", "Extensions"),
    ("health", "Health"),
    ("command", "Command"),
)

#: Offered by `t`, in the order it cycles them. The same set MovieBox lists,
#: which is where the look of this screen comes from.
THEMES: tuple[str, ...] = (
    "textual-dark",
    "catppuccin-mocha",
    "nord",
    "dracula",
    "gruvbox",
    "tokyo-night",
)

DEFAULT_OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Music", "SpotiFLAC")


class SpotiFLACTui(App[None]):
    """The whole terminal UI."""

    TITLE = "SpotiFLAC"
    SUB_TITLE = "terminal UI"
    CSS_PATH = "spotiflac.tcss"

    BINDINGS = [
        Binding("ctrl+r", "start_download", "Run", priority=True),
        Binding("ctrl+c", "stop_download", "Stop", priority=True),
        Binding("ctrl+l", "toggle_log", "Log"),
        Binding("slash", "search", "Search"),
        Binding("question_mark", "help", "Help"),
        Binding("t", "cycle_theme", "Theme", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("q", "request_quit", "Quit"),
    ]

    def __init__(
        self,
        state: ConfigState | None = None,
        min_trust_tier: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.state = state or ConfigState(output_dir=DEFAULT_OUTPUT_DIR)
        # Forwarded from --min-trust-tier: without it, an install started
        # from the Extensions panel would fall back to $SPOTIFLAC_MIN_TRUST
        # and ignore what the operator typed on this very command line.
        self._min_trust_tier = min_trust_tier
        self._runner: DownloadRunner | None = None
        # Not `_running`: textual.app.App uses that name for "the app is
        # running" and sets it True on startup, so a guard reading it would
        # have refused every download and every quit.
        self._download_running = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield ListView(
                *[ListItem(Label(label), id=f"mode-{key}") for key, label in MODES],
                id="sidebar",
            )
            with Vertical(id="main"):
                with ContentSwitcher(initial="download", id="panels"):
                    yield ConfigPanel(self.state, id="download")
                    yield SearchPanel(id="search")
                    yield QueuePanel(id="queue")
                    yield SessionPanel(lambda: self.state, id="session")
                    yield ExtensionsPanel(self._min_trust_tier, id="extensions")
                    yield HealthPanel(id="health")
                    yield Static(id="command", classes="command-panel")
                yield Container(
                    RichLog(id="log", wrap=True, markup=False, max_lines=2000),
                    id="log-pane",
                )
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = THEMES[0]
        self.query_one("#log-pane").display = False
        self.query_one("#sidebar", ListView).index = 0
        self._refresh_command_panel()
        self._set_status("Ctrl+R to start · Ctrl+L for the log · q to quit")
        self.run_worker(self._restore_last_folder(), exclusive=False)

    async def _restore_last_folder(self) -> None:
        """Offers last run's folder, the way the wizard pre-filled it.

        Best-effort by design: session memory is a convenience, and a missing
        or unreadable one is not worth a message — the default is already a
        sensible place for music to land.
        """
        if self.state.output_dir != DEFAULT_OUTPUT_DIR:
            return
        try:
            from ..core.session_memory import get_last_folder_async

            folder = await get_last_folder_async()
        except Exception:
            return
        if not folder:
            return
        self.state.output_dir = folder
        try:
            from textual.widgets import Input

            self.query_one("#cfg-output_dir", Input).value = folder
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    @on(ListView.Highlighted, "#sidebar")
    def _switch_panel(self, event: ListView.Highlighted) -> None:
        if event.item is None or not event.item.id:
            return
        self.query_one("#panels", ContentSwitcher).current = event.item.id.removeprefix(
            "mode-",
        )

    @on(ConfigPanel.Changed)
    def _config_changed(self, event: ConfigPanel.Changed) -> None:
        self.state = event.state
        self._refresh_command_panel()

    @on(SessionPanel.UrlChosen)
    def _adopt_url(self, event: SessionPanel.UrlChosen) -> None:
        """Writes the URL into the form rather than only into the state.

        Going through the Input is what makes the change visible and what
        raises `ConfigPanel.Changed`, so the CLI preview and the readiness
        banner update the same way they would if it had been typed.
        """
        self.query_one("#cfg-url", Input).value = event.url
        self._show_panel("download")

    @on(SearchPanel.UrlChosen)
    def _adopt_search_result(self, event: SearchPanel.UrlChosen) -> None:
        self.query_one("#cfg-url", Input).value = event.url
        self._show_panel("download")
        self._set_status(f"Downloading “{event.label}” — Ctrl+R to start.")

    @on(SessionPanel.StateLoaded)
    async def _adopt_state(self, event: SessionPanel.StateLoaded) -> None:
        """A loaded profile replaces every setting, so the form is rebuilt.

        Cheaper than it looks, and far safer than assigning ~40 widget values
        one by one: a control missed in that loop would keep showing the old
        profile while the state held the new one.
        """
        self.state = event.state
        switcher = self.query_one("#panels", ContentSwitcher)
        await self.query_one("#download", ConfigPanel).remove()
        await switcher.mount(ConfigPanel(self.state, id="download"))
        switcher.current = "download"
        self._show_panel("download")
        self._refresh_command_panel()
        name = event.state.profile_loaded or "profile"
        self._set_status(f"Loaded {name}.")

    def _show_panel(self, key: str) -> None:
        """Moves the sidebar, and lets its handler switch the panel.

        Going through the sidebar rather than the ContentSwitcher keeps the
        highlight and the visible panel in step — setting the switcher alone
        leaves the sidebar pointing at whatever it pointed at before.
        """
        keys = [mode for mode, _label in MODES]
        if key in keys:
            self.query_one("#sidebar", ListView).index = keys.index(key)

    def _refresh_command_panel(self) -> None:
        panel = self.query_one("#command", Static)
        problems = self.state.missing_requirements()
        if problems:
            panel.update(
                "Not runnable yet — still needed: " + ", ".join(problems) + "\n\n"
                "The command appears here as soon as the run has everything "
                "it needs.",
            )
            return
        panel.update(
            "The same run, as a command you can script or schedule:\n\n"
            f"    {self.state.cli_command()}\n",
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _write_log(self, line: str, severity: str = "") -> None:
        self.query_one("#log", RichLog).write(line)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_toggle_log(self) -> None:
        pane = self.query_one("#log-pane")
        pane.display = not pane.display

    def action_search(self) -> None:
        """`/` goes to the search box, wherever you were."""
        self._show_panel("search")
        self.query_one("#search", SearchPanel).focus_query()

    def action_help(self) -> None:
        # Pushed rather than toggled: `?` inside the modal is bound to
        # dismiss, so a second press closes it either way.
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_cycle_theme(self) -> None:
        current = THEMES.index(self.theme) if self.theme in THEMES else 0
        self.theme = THEMES[(current + 1) % len(THEMES)]

    def action_cursor_down(self) -> None:
        self.screen.focus_next()

    def action_cursor_up(self) -> None:
        self.screen.focus_previous()

    def action_request_quit(self) -> None:
        if self._download_running:
            self._set_status("A download is running — Ctrl+C stops it first.")
            return
        self.exit()

    def action_start_download(self) -> None:
        if self._download_running:
            self._set_status("Already running. Ctrl+C to stop.")
            return

        problems = self.state.missing_requirements()
        if problems:
            self._set_status("Cannot start — still needed: " + ", ".join(problems))
            self._show_panel("download")
            return

        self._download_running = True
        self.query_one("#queue", QueuePanel).reset()
        self._show_panel("queue")
        self.query_one("#log-pane").display = True
        self._set_status("Starting…")
        self.run_worker(self._download(), exclusive=True, group="download")

    def action_stop_download(self) -> None:
        if not self._download_running:
            self.exit()
            return
        self.workers.cancel_group(self, "download")
        self._download_running = False
        self._set_status("Stopped.")

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------

    async def _download(self) -> None:
        cfg = self.state.to_cfg()
        log_level = logging.DEBUG if self.state.verbose else logging.INFO
        runner = DownloadRunner(cfg, log_level)
        self._runner = runner

        queue = self.query_one("#queue", QueuePanel)
        try:
            async for kind, payload, severity in runner.events():
                if kind == OUTPUT:
                    self._write_log(str(payload), severity)
                elif kind == STATS:
                    queue.apply_stats(payload)
                    self._set_status(_status_for(payload))
                elif kind in (FINISHED, FAILED):
                    self._set_status(_outcome_line(payload))
        finally:
            self._download_running = False
            self._runner = None
            await self._remember_folder()

    async def _remember_folder(self) -> None:
        try:
            from ..core.session_memory import set_last_folder_async

            await set_last_folder_async(self.state.output_dir)
        except Exception:
            pass


def _status_for(stats: dict[str, Any]) -> str:
    from .runner import make_status_line

    return make_status_line(stats) or "Running…"


def _outcome_line(outcome) -> str:
    if getattr(outcome, "error", ""):
        return f"Finished with an error — {outcome.error}"
    parts = [f"{outcome.completed} downloaded"]
    if outcome.failed:
        parts.append(f"{outcome.failed} failed")
    if outcome.skipped:
        parts.append(f"{outcome.skipped} skipped")
    return "Done · " + " · ".join(parts)


async def run_tui_async(
    state: ConfigState | None = None,
    min_trust_tier: str | None = None,
) -> None:
    """Entry point for `--tui`, awaited by the launcher.

    `run_async()`, not `run()`: `launcher.amain()` is itself running under
    `asyncio.run()`, and Textual's `run()` opens a second loop — which raises
    "asyncio.run() cannot be called from a running event loop" before a
    single frame is drawn. `--gui` gets away with the sync call next door
    because pywebview has no loop of its own.
    """
    await SpotiFLACTui(state, min_trust_tier).run_async()


def run_tui(
    state: ConfigState | None = None,
    min_trust_tier: str | None = None,
) -> None:
    """The same, for a caller that has no event loop of its own."""
    SpotiFLACTui(state, min_trust_tier).run()

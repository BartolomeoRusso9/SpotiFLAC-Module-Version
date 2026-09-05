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
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    ContentSwitcher,
    Input,
    ListItem,
    ListView,
    Label,
    RichLog,
    Static,
)

from .banner import Banner, HintBar
from ..core.paths import default_download_dir
from .branding import notice, panel_tag, panel_title, pointer

from .config_state import ConfigState
from .config_view import ConfigPanel
from .extensions_view import ExtensionsPanel
from .health_view import HealthPanel
from .help_screen import HelpScreen
from .queue_view import QueuePanel
from .search_view import SearchPanel
from .tracklist_view import TracklistPanel
from .session_view import SessionPanel
from .runner import FAILED, FINISHED, OUTPUT, STATS, DownloadRunner

#: Sidebar entries: (id, label), in the order the work is usually done.
MODES: tuple[tuple[str, str], ...] = (
    ("download", "Download"),
    ("search", "Search"),
    ("tracks", "Tracks"),
    ("queue", "Queue"),
    ("session", "Session"),
    ("extensions", "Extensions"),
    ("health", "Health"),
    ("command", "Command"),
)

#: The nine MovieBox offers, in its order, cycled by `t`. Mocha first
#: because that is its default and the palette this screen was drawn against.
THEMES: tuple[str, ...] = (
    "catppuccin-mocha",
    "catppuccin-latte",
    "catppuccin-macchiato",
    "catppuccin-frappe",
    "nord",
    "tokyo-night",
    "dracula",
    "gruvbox",
    "rose-pine",
)

#: Panel id -> (title, the slash-command tag on the right). MovieBox puts a
#: command in every card's top-right corner; here it names the CLI flag or
#: shortcut that does the same job, which is the same favour.
PANEL_TITLES: dict[str, tuple[str, str]] = {
    "download": ("Configuration", "Ctrl+R to run"),
    "search": ("Search", "/"),
    "tracks": ("Tracks", "space to pick"),
    "queue": ("Queue", "live"),
    "session": ("Session", "history · profiles"),
    "extensions": ("Extensions", "registries"),
    "health": ("Health", "--health-check"),
    "command": ("Equivalent command", "copy me"),
}

#: `~/Music/SpotiFLAC`, from `core.paths` so the desktop window and this
#: agree. Two frontends with two defaults is two libraries on one machine.
DEFAULT_OUTPUT_DIR = default_download_dir()


class SpotiFLACTui(App[None]):
    """The whole terminal UI."""

    TITLE = "SpotiFLAC"
    SUB_TITLE = "terminal UI"
    CSS_PATH = "spotiflac.tcss"

    BINDINGS = [
        Binding("ctrl+r", "start_download", "Run", priority=True),
        Binding("ctrl+c", "stop_download", "Stop", priority=True),
        # priority: a focused widget must never be able to swallow this.
        # Safe here in a way `j`/`q`/`t` are not — Ctrl+L types nothing.
        Binding("ctrl+l", "toggle_log", "Log", priority=True),
        Binding("escape", "close_log", "Close log", show=False),
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
        self.state = state or ConfigState()
        # Forwarded from --min-trust-tier: without it, an install started
        # from the Extensions panel would fall back to $SPOTIFLAC_MIN_TRUST
        # and ignore what the operator typed on this very command line.
        self._min_trust_tier = min_trust_tier
        self._runner: DownloadRunner | None = None
        # Not `_running`: textual.app.App uses that name for "the app is
        # running" and sets it True on startup, so a guard reading it would
        # have refused every download and every quit.
        self._download_running = False
        #: Severity words waiting for their toast widget to mount.
        self._pending_toast_labels: list[str] = []

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        with Horizontal(id="body"):
            mark = pointer()
            yield ListView(
                *[
                    ListItem(Label(f" {mark} {label}"), id=f"mode-{key}")
                    for key, label in MODES
                ],
                id="sidebar",
            )
            with Vertical(id="main"):
                with ContentSwitcher(initial="download", id="panels"):
                    yield ConfigPanel(self.state, id="download")
                    yield SearchPanel(id="search")
                    yield TracklistPanel(lambda: self.state.url, id="tracks")
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
        yield HintBar(id="hints")

    def on_mount(self) -> None:
        self.theme = THEMES[0]
        self._decorate_panels()
        self.query_one("#log-pane").display = False
        self.query_one("#sidebar", ListView).index = 0
        self._refresh_command_panel()
        self._set_status("Pick a URL and press Ctrl+R.", "info")


    def _decorate_panels(self) -> None:
        """Gives every card MovieBox's title: a marker, a name, a tag right."""
        self.query_one("#log-pane").border_title = panel_title("Log")
        for panel_id, (_title, tag) in PANEL_TITLES.items():
            try:
                panel = self.query_one(f"#{panel_id}")
            except Exception:
                continue
            panel.border_subtitle = panel_tag(tag)
        self._refresh_panel_markers()

    def _panel_facts(self, panel_id: str) -> list[str]:
        """What is worth putting in a pane's title besides its name."""
        try:
            if panel_id == "tracks":
                tracks = self.query_one("#tracks", TracklistPanel)
                if tracks.has_selection:
                    chosen = len(tracks.selected_indices())
                    return [f"{chosen}/{len(tracks.tracklist)}"]
            elif panel_id == "download":
                return [] if self.state.is_runnable else ["not ready"]
            elif panel_id == "queue":
                queue = self.query_one("#queue", QueuePanel)
                if queue._rows:
                    return [f"{len(queue._rows)} track(s)"]
        except Exception:
            return []
        return []

    def _refresh_panel_markers(self) -> None:
        """Fills the dot on whichever pane the keyboard is pointing at.

        Called on every focus change, which sounds expensive and is not: it
        rewrites at most nine short strings, and the alternative — a
        highlight you have to go looking for — is the thing MovieBox's filled
        dot exists to avoid.
        """
        focused = self.focused
        for panel_id, (title, _tag) in PANEL_TITLES.items():
            try:
                panel = self.query_one(f"#{panel_id}")
            except Exception:
                continue
            live = focused is not None and (
                focused is panel or panel in focused.ancestors
            )
            panel.border_title = panel_title(
                title,
                *self._panel_facts(panel_id),
                focused=live,
            )

        try:
            sidebar = self.query_one("#sidebar", ListView)
        except Exception:
            return
        sidebar.border_title = panel_title("Modes", focused=focused is sidebar)

    def on_descendant_focus(self) -> None:
        self._refresh_panel_markers()

    def on_descendant_blur(self) -> None:
        self._refresh_panel_markers()

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

    @on(TracklistPanel.SelectionChanged)
    def _selection_changed(self, event: TracklistPanel.SelectionChanged) -> None:
        if event.selected == event.total:
            self._set_status(f"All {event.total} tracks — Ctrl+R to start.")
        elif event.selected == 0:
            self._set_status("No tracks selected — nothing would download.", "warning")
        else:
            self._set_status(
                f"{event.selected} of {event.total} tracks — Ctrl+R to start.",
            )
        # The count lives in the pane's title too, so it stays visible once
        # you have moved on to another panel.
        self._refresh_panel_markers()
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
        self._announce(f"“{event.label}” is the target — Ctrl+R to start.", "success")

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
        self._announce(f"Loaded {name}.", "success")

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
        lines = [
            "The same run, as a command you can script or schedule:",
            "",
            f"    {self.state.cli_command()}",
        ]

        # A partial selection has no command-line equivalent: the CLI takes a
        # link and fetches what is behind it. Showing the whole-album command
        # while three tracks are ticked would be a quietly wrong answer, so
        # say which part the command does not carry.
        try:
            tracks = self.query_one("#tracks", TracklistPanel)
        except Exception:
            tracks = None
        if tracks is not None and tracks.has_selection and not tracks.is_whole_collection:
            chosen = len(tracks.selected_indices())
            lines += [
                "",
                f"Note: {chosen} of {len(tracks.tracklist)} tracks are picked on "
                "the Tracks panel. The command above fetches the whole link — "
                "picking tracks is something only this screen and the desktop "
                "window can do.",
            ]
        panel.update("\n".join(lines) + "\n")

    #: kind → what Textual calls that severity, and what MovieBox labels it.
    _SEVERITIES = {
        "info": ("information", "INFO"),
        "success": ("information", "DONE"),
        "warning": ("warning", "WARNING"),
        "error": ("error", "ERROR"),
    }

    def _set_status(self, text: str, kind: str = "info") -> None:
        """One status line, marked the way MovieBox marks its notices."""
        status = self.query_one("#status", Static)
        message, css = notice(kind, text)
        status.update(message)
        for candidate in ("notice-info", "notice-success", "notice-warning", "notice-error"):
            status.set_class(candidate == css, candidate)

    def _toast(self, text: str, kind: str = "info", title: str = "") -> None:
        """A notice that appears, is read, and goes away.

        The status line says what is true right now — how a run is going,
        what is still missing. A toast says what just happened. MovieBox
        keeps the two apart and so does this: a message that scrolls the
        standing state off the screen has cost you the thing you were
        watching.
        """
        severity, label = self._SEVERITIES.get(kind, self._SEVERITIES["info"])
        # No title on the notification itself: Textual renders it as the
        # toast's first line, and it belongs on the border. Queued instead,
        # to be claimed by the widget once it mounts.
        self._pending_toast_labels.append(title or label)
        self.notify(text, severity=severity, timeout=6)
        self.call_after_refresh(self._label_toast_borders)

    def _label_toast_borders(self) -> None:
        """Moves a toast's severity from its first line onto its border.

        Which is where MovieBox puts it, and it buys back a line in a box
        that is three tall. Textual gives no hook for this — `ToastRack`
        builds its `Toast` widgets itself — so this reaches for the widget
        class from a private module and stops quietly if a future version
        moves it. Failing here costs the border label and nothing else: the
        toast still carries the message, the colour and the border that say
        which kind it is.

        Labels are handed out in the order they were queued, because toasts
        mount in the order they were raised and the widget carries nothing
        that would tell `DONE` from `INFO` — Textual has one severity for
        both.
        """
        try:
            from textual.widgets._toast import Toast
        except Exception:
            self._pending_toast_labels.clear()
            return
        if not self._pending_toast_labels:
            return
        for toast in self.screen.query(Toast):
            if toast.border_title or not self._pending_toast_labels:
                continue
            toast.border_title = self._pending_toast_labels.pop(0)

    def _announce(self, text: str, kind: str = "info") -> None:
        """Says it in both places: it is both news and the current state."""
        self._set_status(text, kind)
        self._toast(text, kind)

    def _write_log(self, line: str, severity: str = "") -> None:
        self.query_one("#log", RichLog).write(line)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_toggle_log(self) -> None:
        self._set_log_visible(not self.query_one("#log-pane").display)

    def action_close_log(self) -> None:
        """Esc closes it, for when Ctrl+L is spoken for elsewhere.

        Terminal multiplexers and a few terminals claim Ctrl+L for their own
        clear-screen, and a pane you can open but not close is worse than no
        pane at all.
        """
        if self.query_one("#log-pane").display:
            self._set_log_visible(False)

    def _set_log_visible(self, visible: bool) -> None:
        """Shows or hides the log, and never leaves the focus inside it.

        Textual drops focus entirely when the focused widget is hidden, so
        closing the log while reading it left nothing focused at all — no
        cursor, no arrow keys, an interface that looks broken. Focus goes
        back to the sidebar, which is somewhere you can always steer from.
        """
        pane = self.query_one("#log-pane")
        focus_was_inside = self.focused is not None and (
            self.focused is pane or pane in self.focused.ancestors
        )
        pane.display = visible
        if not visible and focus_was_inside:
            self.query_one("#sidebar", ListView).focus()

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
            self._announce("A download is running — Ctrl+C stops it first.", "warning")
            return
        self.exit()

    def action_start_download(self) -> None:
        if self._download_running:
            self._set_status("Already running. Ctrl+C to stop.", "warning")
            return

        problems = self.state.missing_requirements()
        if problems:
            self._announce(
                "Cannot start — still needed: " + ", ".join(problems),
                "warning",
            )
            self._show_panel("download")
            return

        tracks = self.query_one("#tracks", TracklistPanel)
        if tracks.has_selection and not tracks.selected_indices():
            self._announce(
                "Nothing selected on the Tracks panel — pick at least one, "
                "or press a for all.",
                "warning",
            )
            self._show_panel("tracks")
            return

        self._download_running = True
        self.query_one("#queue", QueuePanel).reset()
        self._show_panel("queue")
        self._set_log_visible(True)
        self._set_status("Starting…")
        self.run_worker(self._download(), exclusive=True, group="download")

    def action_stop_download(self) -> None:
        if not self._download_running:
            self.exit()
            return
        self.workers.cancel_group(self, "download")
        self._download_running = False
        self._announce("Stopped.", "warning")

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------

    def _download_target(self):
        """The URL, or the tracks picked from it.

        `None` when the Tracks panel has nothing to say — an unloaded panel
        must not turn a perfectly good link into an empty list.
        """
        from ..core.tracklist import download_target, unresolved_titles

        panel = self.query_one("#tracks", TracklistPanel)
        if not panel.has_selection:
            return None, []
        selected = panel.selected_indices()
        if not selected:
            return [], []
        return (
            download_target(panel.tracklist, selected),
            unresolved_titles(panel.tracklist, selected),
        )

    async def _download(self) -> None:
        cfg = self.state.to_cfg()
        target, unresolved = self._download_target()
        if target is not None:
            cfg["url"] = target
            cfg["csv_path"] = ""
        if unresolved:
            self._write_log(
                f"{len(unresolved)} selected track(s) carry no link of their "
                f"own and will be skipped: {', '.join(unresolved[:5])}"
                + ("…" if len(unresolved) > 5 else ""),
                "warn",
            )
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
                    self._announce(
                        _outcome_line(payload),
                        "error" if kind == FAILED else "success",
                    )
        finally:
            self._download_running = False
            self._runner = None
            await self._remember_folder()

    async def _remember_folder(self) -> None:
        """Records where this run landed, for the other frontends.

        Not read back on the way in: `~/Music/SpotiFLAC` is the default and
        stays the default, so a one-off download somewhere else does not
        quietly become where everything goes next. The wizard's old
        folder-memory did read it back, which is exactly the surprise.
        """
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

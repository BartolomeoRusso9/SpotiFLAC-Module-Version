"""session_view.py — What the last runs left behind: history and profiles.

Two of the wizard's optional sections, which are the parts of it people
actually came back for: the URL you fetched last week, and the settings you
saved so you would not have to answer fifteen questions again.

Both are plain async functions in `core/` — `get_recent_fetches()`,
`list_profiles_async()` and friends — so this panel is mostly a list and two
buttons. It deliberately does not go through `SpotiFLAC.app`: that module
wraps the same calls in threads for pywebview's benefit, and there is no
benefit here.

Loading a profile replaces the whole configuration, so it announces itself
through :class:`SessionPanel.StateLoaded` rather than editing the panel that
owns the state; the app rebuilds the form from the new state.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .config_state import ConfigState

_HISTORY_SHOWN = 12


class SessionPanel(VerticalScroll):
    """Recent URLs and saved profiles, in one place."""

    BORDER_TITLE = "Session"

    class UrlChosen(Message):
        """A history entry was picked; the URL should be adopted."""

        def __init__(self, url: str) -> None:
            super().__init__()
            self.url = url

    class StateLoaded(Message):
        """A profile was loaded; the whole configuration is replaced."""

        def __init__(self, state: ConfigState) -> None:
            super().__init__()
            self.state = state

    def __init__(self, get_state, **kwargs) -> None:
        super().__init__(**kwargs)
        # A callable rather than the state itself: saving a profile has to
        # capture what the configuration is *now*, not what it was when this
        # panel was built.
        self._get_state = get_state
        self._profiles: list[str] = []

    def compose(self) -> ComposeResult:
        yield Label("Recent URLs", classes="section-heading")
        yield OptionList(id="history-list")

        yield Label("Profiles", classes="section-heading")
        yield OptionList(id="profile-list")
        yield Horizontal(
            Input(placeholder="profile name", id="profile-name"),
            Button("Save", id="profile-save", variant="primary"),
            Button("Delete", id="profile-delete", variant="warning"),
            classes="setting-row",
        )
        yield Static("", id="session-status")

    def on_mount(self) -> None:
        self.run_worker(self.refresh_all(), exclusive=False)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    async def refresh_all(self) -> None:
        await self._load_history()
        await self._load_profiles()

    async def _load_history(self) -> None:
        try:
            from ..core.history import get_recent_fetches

            entries = get_recent_fetches()
        except Exception:
            entries = []

        listing = self.query_one("#history-list", OptionList)
        listing.clear_options()
        for entry in entries[:_HISTORY_SHOWN]:
            url = str(entry.get("url") or "")
            if not url:
                continue
            label = str(entry.get("label") or url)
            # The id is the URL itself: the list is short, and it saves
            # keeping a parallel index in step with a list that reloads.
            listing.add_option(
                Option(label if label == url else f"{label}\n  {url}", id=url),
            )
        if not listing.option_count:
            listing.add_option(Option("Nothing fetched yet", id=""))

    async def _load_profiles(self) -> None:
        try:
            from ..core.profiles import list_profiles_async

            self._profiles = await list_profiles_async()
        except Exception:
            self._profiles = []

        listing = self.query_one("#profile-list", OptionList)
        listing.clear_options()
        for name in self._profiles:
            listing.add_option(Option(name, id=name))
        if not listing.option_count:
            listing.add_option(Option("No profiles saved", id=""))

    # ------------------------------------------------------------------
    # Acting
    # ------------------------------------------------------------------

    def _say(self, message: str) -> None:
        self.query_one("#session-status", Static).update(message)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = event.option.id
        if not chosen:
            return
        if event.option_list.id == "history-list":
            self.post_message(self.UrlChosen(chosen))
            self._say(f"Using {chosen}")
        elif event.option_list.id == "profile-list":
            self.query_one("#profile-name", Input).value = chosen
            self.run_worker(self._load_profile(chosen), exclusive=False)

    async def _load_profile(self, name: str) -> None:
        try:
            from ..core.profiles import get_profile_async

            data = await get_profile_async(name)
        except Exception as exc:
            self._say(f"Could not read '{name}' — {exc}")
            return
        if not data:
            self._say(f"Profile '{name}' is empty")
            return

        # Profiles store the wizard's cfg, underscore-prefixed bookkeeping
        # included; `from_cfg` keeps what it recognises and drops the rest.
        state = ConfigState.from_cfg(data)
        state.profile_loaded = name
        self.post_message(self.StateLoaded(state))
        self._say(f"Loaded '{name}'")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        name = self.query_one("#profile-name", Input).value.strip()
        if not name:
            self._say("Name the profile first.")
            return
        if event.button.id == "profile-save":
            self.run_worker(self._save_profile(name), exclusive=False)
        elif event.button.id == "profile-delete":
            self.run_worker(self._delete_profile(name), exclusive=False)

    async def _save_profile(self, name: str) -> None:
        try:
            from ..core.profiles import save_profile_async

            await save_profile_async(name, self._get_state().to_cfg())
        except Exception as exc:
            self._say(f"Could not save '{name}' — {exc}")
            return
        await self._load_profiles()
        self._say(f"Saved '{name}'")

    async def _delete_profile(self, name: str) -> None:
        try:
            from ..core.profiles import delete_profile_async

            removed = await delete_profile_async(name)
        except Exception as exc:
            self._say(f"Could not delete '{name}' — {exc}")
            return
        await self._load_profiles()
        self._say(f"Deleted '{name}'" if removed else f"No profile called '{name}'")

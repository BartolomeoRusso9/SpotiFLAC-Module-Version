"""config_view.py — The wizard's fifteen questions as one editable screen.

Every control here is bound to a field of :class:`ConfigState` by its `id`:
a widget with `id="cfg-output_dir"` writes to `state.output_dir` and nothing
else. That is the whole binding layer, and it is deliberately dumb — the
rules about which settings cancel which live in `ConfigState.normalized()`,
so a control cannot enforce a rule the produced `cfg` would then contradict.

What the widgets *do* own is whether a question is worth showing: a bitrate
for a lossless target, or an artist separator when only the first artist is
kept, are settings with no effect, and the panel disables them rather than
inviting an answer it will discard.

One difference from the wizard worth naming: quality is offered as the six
canonical tiers rather than a provider-specific menu. The wizard could ask a
narrower question because it already knew which providers had been picked;
a screen where both are editable at once cannot, and `allow_fallback` —
which is on by default — is what covers a tier a provider will not serve.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    Label,
    Select,
    SelectionList,
    Static,
    Switch,
)

from ..core.transcode import TRANSCODE_CHOICES
from .branding import quality_badge
from ..extensions.catalog import installed_service_ids
from .config_state import (
    ENRICH_PROVIDERS,
    LYRICS_PROVIDERS,
    POST_DOWNLOAD_ACTIONS,
    TRANSCODE_BITRATES,
    ConfigState,
)

_FIELD_PREFIX = "cfg-"

#: Canonical tiers, best first, with what each one actually means.
QUALITY_CHOICES: tuple[tuple[str, str], ...] = (
    ("Hi-Res Lossless — best available anywhere", "HI_RES_LOSSLESS"),
    ("Hi-Res — 24-bit where the provider has it", "HI_RES"),
    ("Lossless — CD quality FLAC/ALAC", "LOSSLESS"),
    ("Dolby Atmos — Tidal only", "DOLBY_ATMOS"),
    ("High — lossy, largest", "HIGH"),
    ("Low — lossy, smallest", "LOW"),
)

#: Fields whose value is an int, so the Input has to be parsed rather than
#: assigned. A blank one means "unset", which for `loop`/`watch` is None and
#: for the rest is the field's default.
_INT_FIELDS = {
    "track_max_retries": 0,
    "max_concurrent_downloads": 2,
    "timeout_s": 180,
}
_OPTIONAL_INT_FIELDS = ("loop", "watch")


def _field_id(name: str) -> str:
    return f"{_FIELD_PREFIX}{name}"


def _field_id_selector(name: str) -> str:
    return f"#{_field_id(name)}"


def _field_name(widget_id: str | None) -> str | None:
    if not widget_id or not widget_id.startswith(_FIELD_PREFIX):
        return None
    return widget_id[len(_FIELD_PREFIX) :]


class Row(Horizontal):
    """A label and its control, side by side."""

    def __init__(self, label: str, control, hint: str = "") -> None:
        super().__init__(classes="setting-row")
        self._label = label
        self._control = control
        self._hint = hint

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="setting-label")
        yield self._control
        if self._hint:
            yield Label(self._hint, classes="setting-hint")


class ConfigPanel(VerticalScroll):
    """The editable view onto a :class:`ConfigState`."""

    BORDER_TITLE = "Configuration"

    class Changed(Message):
        """Something was edited. Carries the state, already normalised."""

        def __init__(self, state: ConfigState) -> None:
            super().__init__()
            self.state = state

    def __init__(self, state: ConfigState | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state or ConfigState()
        self._known_fields = {f.name for f in dataclass_fields(ConfigState)}

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        state = self.state

        with Collapsible(title="Source", collapsed=False):
            yield Row(
                "URL",
                Input(
                    value=state.url,
                    placeholder="https://open.spotify.com/…",
                    id=_field_id("url"),
                ),
            )
            yield Row(
                "CSV track list",
                Input(
                    value=state.csv_path,
                    placeholder="a .csv or .tsv on this machine",
                    id=_field_id("csv_path"),
                ),
                hint="wins over the URL",
            )
            yield Button("Browse for a track list…", id="csv-browse")

        with Collapsible(title="Destination", collapsed=False):
            yield Row(
                "Folder",
                Input(value=state.output_dir, id=_field_id("output_dir")),
            )
            yield Row(
                "Exact file path",
                Input(
                    value=state.output_path or "",
                    placeholder="leave blank to name files normally",
                    id=_field_id("output_path"),
                ),
            )

        with Collapsible(title="Providers & quality", collapsed=False):
            installed = installed_service_ids()
            yield Label("Providers, in order of preference", classes="setting-label")
            yield SelectionList[str](
                *[
                    (service, service, service in state.services)
                    for service in installed
                ],
                id=_field_id("services"),
            )
            # "None selected" and "none installed" need different advice, and
            # only the second one is a dead end: the wizard used to refuse to
            # start at all here, which was right — an empty provider list is
            # not a choice the user got wrong, it is a setup step they have
            # not done. Saying where to do it is the useful half.
            yield Label(
                "No download provider is installed — add a registry in the "
                "Extensions panel, or run spotiflac --help for the registry "
                "flags.",
                id="no-providers",
                classes="blocking",
            )
            yield Row(
                "Quality",
                Select(
                    QUALITY_CHOICES,
                    value=state.quality,
                    allow_blank=False,
                    id=_field_id("quality"),
                ),
            )
            # The chosen tier, as MovieBox shows a resolution: a short label
            # on a solid colour, so the most consequential setting on this
            # screen is legible without reading the dropdown.
            yield Label("", id="quality-badge", markup=False)
            yield Row(
                "Allow quality fallback",
                Switch(value=state.allow_fallback, id=_field_id("allow_fallback")),
            )

        with Collapsible(title="Transcoding", collapsed=True):
            yield Row(
                "Convert to",
                Select(
                    [(label, value or "") for label, value in TRANSCODE_CHOICES],
                    value=state.transcode_to or "",
                    allow_blank=False,
                    id=_field_id("transcode_to"),
                ),
                hint="needs ffmpeg",
            )
            yield Row(
                "Bitrate",
                Select(
                    [(rate, rate) for rate in TRANSCODE_BITRATES],
                    value=state.transcode_bitrate,
                    allow_blank=False,
                    id=_field_id("transcode_bitrate"),
                ),
            )
            yield Row(
                "Keep the original too",
                Switch(
                    value=state.transcode_keep_original,
                    id=_field_id("transcode_keep_original"),
                ),
            )

        with Collapsible(title="Naming & organisation", collapsed=True):
            yield Row(
                "Filename format",
                Input(
                    value=state.filename_format,
                    id=_field_id("filename_format"),
                ),
            )
            yield Row(
                "Number the files",
                Switch(
                    value=state.use_track_numbers,
                    id=_field_id("use_track_numbers"),
                ),
                hint="excludes subfolders",
            )
            yield Row(
                "Use the album's own numbering",
                Switch(
                    value=state.use_album_track_numbers,
                    id=_field_id("use_album_track_numbers"),
                ),
            )
            yield Row(
                "Artist subfolders",
                Switch(
                    value=state.use_artist_subfolders,
                    id=_field_id("use_artist_subfolders"),
                ),
            )
            yield Row(
                "Album subfolders",
                Switch(
                    value=state.use_album_subfolders,
                    id=_field_id("use_album_subfolders"),
                ),
            )
            yield Row(
                "Playlist subfolders",
                Switch(
                    value=state.create_playlist_subfolders,
                    id=_field_id("create_playlist_subfolders"),
                ),
            )
            yield Row(
                "First artist only",
                Switch(
                    value=state.first_artist_only,
                    id=_field_id("first_artist_only"),
                ),
            )
            yield Row(
                "Artist separator",
                Input(
                    value=state.artist_separator or "",
                    placeholder="blank = standard multi-value tags",
                    id=_field_id("artist_separator"),
                ),
            )
            yield Row(
                "Keep featured artists",
                Switch(
                    value=state.include_featuring,
                    id=_field_id("include_featuring"),
                ),
            )

        with Collapsible(title="Lyrics", collapsed=True):
            yield Row(
                "Embed synced lyrics",
                Switch(value=state.embed_lyrics, id=_field_id("embed_lyrics")),
            )
            yield Label("Lyrics providers, in order", classes="setting-label")
            yield SelectionList[str](
                *[
                    (provider, provider, provider in state.lyrics_providers)
                    for provider in LYRICS_PROVIDERS
                ],
                id=_field_id("lyrics_providers"),
            )
            yield Row(
                "Apple lyrics word-by-word",
                Switch(
                    value=state.apple_lyrics_word_by_word,
                    id=_field_id("apple_lyrics_word_by_word"),
                ),
            )
            yield Row(
                "Save an .lrc alongside",
                Switch(value=state.save_lrc, id=_field_id("save_lrc")),
            )
            yield Row(
                ".lrc library folder",
                Input(
                    value=state.lrc_library_dir or "",
                    placeholder="collect every .lrc in one place",
                    id=_field_id("lrc_library_dir"),
                ),
            )

        with Collapsible(title="Metadata enrichment", collapsed=True):
            yield Row(
                "Enrich metadata",
                Switch(value=state.enrich_metadata, id=_field_id("enrich_metadata")),
            )
            yield Label("Enrichment providers, in order", classes="setting-label")
            yield SelectionList[str](
                *[
                    (provider, provider, provider in state.enrich_providers)
                    for provider in ENRICH_PROVIDERS
                ],
                id=_field_id("enrich_providers"),
            )

        with Collapsible(title="Reliability", collapsed=True):
            yield Row(
                "Extra retries per track",
                Input(
                    value=str(state.track_max_retries),
                    type="integer",
                    id=_field_id("track_max_retries"),
                ),
            )
            yield Row(
                "Tracks in parallel",
                Input(
                    value=str(state.max_concurrent_downloads),
                    type="integer",
                    id=_field_id("max_concurrent_downloads"),
                ),
                hint="1 = sequential",
            )
            yield Row(
                "Timeout per attempt (s)",
                Input(
                    value=str(state.timeout_s),
                    type="integer",
                    id=_field_id("timeout_s"),
                ),
                hint="0 = none",
            )
            yield Row(
                "Resume interrupted downloads",
                Switch(value=state.resume, id=_field_id("resume")),
            )

        with Collapsible(title="After the run", collapsed=True):
            yield Row(
                "Action",
                Select(
                    [(action, action) for action in POST_DOWNLOAD_ACTIONS],
                    value=state.post_download_action,
                    allow_blank=False,
                    id=_field_id("post_download_action"),
                ),
            )
            yield Row(
                "Command",
                Input(
                    value=state.post_download_command,
                    placeholder="echo 'Done: {succeeded} tracks in {folder}'",
                    id=_field_id("post_download_command"),
                ),
            )
            yield Row(
                "Repeat every N minutes",
                Input(
                    value="" if state.loop is None else str(state.loop),
                    type="integer",
                    id=_field_id("loop"),
                ),
            )
            yield Row(
                "Keep syncing every N minutes",
                Input(
                    value="" if state.watch is None else str(state.watch),
                    type="integer",
                    id=_field_id("watch"),
                ),
                hint="forever",
            )

        with Collapsible(title="Custom endpoints", collapsed=True):
            yield Row(
                "Qobuz local API",
                Input(
                    value=state.qobuz_local_api_url or "",
                    placeholder="blank to skip",
                    id=_field_id("qobuz_local_api_url"),
                ),
            )
            yield Row(
                "Custom Tidal API",
                Input(
                    value=state.tidal_custom_api or "",
                    placeholder="blank to skip",
                    id=_field_id("tidal_custom_api"),
                ),
            )
            yield Row(
                "Verbose logging",
                Switch(value=state.verbose, id=_field_id("verbose")),
            )

        yield Static("", id="config-problems")

    def on_mount(self) -> None:
        self._refresh_dependencies()
        self.query_one("#no-providers", Label).display = not installed_service_ids()

    # ------------------------------------------------------------------
    # Binding
    # ------------------------------------------------------------------

    def _assign(self, name: str, value) -> None:
        if name not in self._known_fields:
            return
        setattr(self.state, name, value)
        self._refresh_dependencies()
        self.post_message(self.Changed(self.state))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "csv-browse":
            return
        # Imported here so the picker's own screen — and the file scan behind
        # it — cost nothing until somebody actually asks to browse.
        from .csv_picker_screen import CsvPickerScreen

        def _chosen(path: str | None) -> None:
            if path:
                # Through the Input, so the change is visible and raises
                # Changed the same way typing it would.
                self.query_one(_field_id_selector("csv_path"), Input).value = path

        self.app.push_screen(CsvPickerScreen(self.state.output_dir), _chosen)

    def on_input_changed(self, event: Input.Changed) -> None:
        name = _field_name(event.input.id)
        if name is None:
            return
        raw = event.value

        if name in _OPTIONAL_INT_FIELDS:
            self._assign(name, int(raw) if raw.strip().isdigit() else None)
        elif name in _INT_FIELDS:
            # A half-typed number is not an error worth reporting; the field
            # simply keeps its default until there is something to read.
            self._assign(name, int(raw) if raw.strip().isdigit() else _INT_FIELDS[name])
        elif name in {
            "output_path",
            "artist_separator",
            "lrc_library_dir",
            "qobuz_local_api_url",
            "tidal_custom_api",
        }:
            self._assign(name, raw or None)
        else:
            self._assign(name, raw)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        name = _field_name(event.switch.id)
        if name is not None:
            self._assign(name, event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        name = _field_name(event.select.id)
        if name is None:
            return
        value = event.value
        if name == "transcode_to":
            # The "no conversion" row carries "" because Select cannot hold
            # None as an ordinary value.
            self._assign(name, str(value) or None)
        else:
            self._assign(name, str(value))

    def on_selection_list_selected_changed(
        self,
        event: SelectionList.SelectedChanged,
    ) -> None:
        name = _field_name(event.selection_list.id)
        if name is not None:
            self._assign(name, list(event.selection_list.selected))

    # ------------------------------------------------------------------
    # Which questions currently apply
    # ------------------------------------------------------------------

    def _set_enabled(self, name: str, enabled: bool) -> None:
        try:
            widget = self.query_one(f"#{_field_id(name)}")
        except Exception:
            return
        widget.disabled = not enabled

    def _refresh_dependencies(self) -> None:
        """Greys out the settings that currently have no effect.

        The same relationships `ConfigState.normalized()` enforces, shown
        rather than applied — a disabled control says "this is decided by
        something else" where a silently-ignored one would just look broken.
        """
        state = self.state

        self._set_enabled("url", not state.uses_csv)
        self._set_enabled("transcode_bitrate", state.bitrate_applies)
        self._set_enabled("transcode_keep_original", bool(state.transcode_to))
        self._set_enabled("artist_separator", state.separator_applies)

        self._set_enabled("use_album_track_numbers", state.use_track_numbers)
        for name in ("use_artist_subfolders", "use_album_subfolders", "first_artist_only"):
            self._set_enabled(name, state.subfolders_apply)
        self._set_enabled("create_playlist_subfolders", state.is_playlist)

        for name in ("lyrics_providers", "save_lrc", "lrc_library_dir"):
            self._set_enabled(name, state.embed_lyrics)
        self._set_enabled(
            "apple_lyrics_word_by_word",
            state.embed_lyrics and "apple" in state.lyrics_providers,
        )
        self._set_enabled("enrich_providers", state.enrich_metadata)
        self._set_enabled(
            "post_download_command",
            state.post_download_action == "command",
        )

        self._show_quality_badge()
        self._show_problems()

    def _show_quality_badge(self) -> None:
        try:
            badge = self.query_one("#quality-badge", Label)
        except Exception:
            return
        text, css = quality_badge(self.state.quality)
        badge.update(text)
        for candidate in (
            "badge-gold",
            "badge-sapphire",
            "badge-teal",
            "badge-lavender",
            "badge-muted",
        ):
            badge.set_class(candidate == css, candidate)

    def _show_problems(self) -> None:
        try:
            banner = self.query_one("#config-problems", Static)
        except Exception:
            return
        problems = self.state.missing_requirements()
        if not installed_service_ids():
            banner.update(
                "No download provider is installed — nothing can be fetched "
                "until a registry is configured.",
            )
            banner.add_class("blocking")
            return
        if problems:
            banner.update("Still needed: " + ", ".join(problems))
            banner.add_class("blocking")
        else:
            banner.update("Ready to download.")
            banner.remove_class("blocking")

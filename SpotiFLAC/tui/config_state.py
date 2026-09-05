"""config_state.py — Everything `--tui` needs to ask, as state rather than steps.

The wizard this replaced was a sequence: ask a question, branch on the
answer, ask the next one. That shape is why answering it took fifteen screens
and why going back meant starting over — and it is the reason a straight port
would have been the wrong move. Here the same ~40 settings are
one dataclass that is always complete and always valid, and the screen is a
view onto it: change a field and every dependent field settles at once, in
any order, as many times as you like.

The dependencies the wizard expressed by *not asking* a question live in
:meth:`ConfigState.normalized` instead — track numbers still turn off the
subfolder options, "no lyrics" still clears the `.lrc` settings — so a UI
that shows every control at once cannot produce a combination the wizard
could not.

:meth:`ConfigState.to_cfg` produces the dict `launcher.amain()` spreads over
`_run_download_async`, key for key. That contract is the whole point of the
module, and `tests/test_tui_config_state.py` pins it against what the
launcher actually reads, so a new download flag cannot be wired into the CLI
and quietly skipped here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from ..core.cli_preview import format_command, is_playlist_url
from ..core.paths import default_download_dir
from ..core.quality import normalize_quality
from ..core.transcode import is_lossless

#: Lyrics providers the wizard offers, in the order it offers them.
LYRICS_PROVIDERS: tuple[str, ...] = (
    "spotify",
    "apple",
    "deezer",
    "genius",
    "netease",
    "qq",
    "youtube",
    "kugou",
    "musixmatch",
    "lrclib",
    "amazon",
)

#: Enrichment providers. SoundCloud is selectable but not on by default.
ENRICH_PROVIDERS: tuple[str, ...] = ("deezer", "apple", "qobuz", "tidal", "soundcloud")

DEFAULT_LYRICS_PROVIDERS: tuple[str, ...] = ("apple", "lrclib")
DEFAULT_ENRICH_PROVIDERS: tuple[str, ...] = ("deezer", "apple", "qobuz", "tidal")

POST_DOWNLOAD_ACTIONS: tuple[str, ...] = ("none", "open_folder", "notify", "command")

#: The tiers worth offering. The canonical list in `core/quality` has six,
#: but three of them are not choices anyone should be making: HI_RES is a
#: Qobuz-only spelling of the same thing, and HIGH/LOW are lossy tiers on a
#: tool whose point is lossless. The wizard offered these three too.
QUALITY_TIERS: tuple[str, ...] = ("LOSSLESS", "HI_RES_LOSSLESS", "DOLBY_ATMOS")

#: Atmos is a Tidal-exclusive stream. `core.quality.quality_for_provider`
#: already turns it into HI_RES_LOSSLESS for every other provider, so asking
#: for it alongside Deezer is not an error — it just means "the best each one
#: has". With Tidal absent altogether it means nothing at all, and the state
#: settles it back to HI_RES_LOSSLESS rather than carrying a dead value.
ATMOS_PROVIDER = "tidal"
TRANSCODE_BITRATES: tuple[str, ...] = ("320k", "256k", "192k", "128k")

DEFAULT_FILENAME_FORMAT = "{title} - {artist}"
DEFAULT_POST_COMMAND = "echo 'Done: {succeeded} tracks in {folder}'"


@dataclass
class ConfigState:
    """One download run's settings, complete from the moment it exists.

    Every field carries the same default the wizard would have offered, so a
    state nobody has touched is already runnable — which is what lets the
    TUI open on a summary instead of on question one.
    """

    # ── What to download ────────────────────────────────────────────────
    url: str = ""
    csv_path: str = ""

    # ── Where it lands ──────────────────────────────────────────────────
    #: `~/Music/SpotiFLAC`, the same default the desktop window uses.
    output_dir: str = field(default_factory=default_download_dir)
    output_path: str | None = None

    # ── Providers and quality ───────────────────────────────────────────
    services: list[str] = field(default_factory=list)
    quality: str = "LOSSLESS"
    allow_fallback: bool = True

    # ── Transcoding ─────────────────────────────────────────────────────
    transcode_to: str | None = None
    transcode_bitrate: str = "320k"
    transcode_keep_original: bool = False

    # ── Naming and organisation ─────────────────────────────────────────
    filename_format: str = DEFAULT_FILENAME_FORMAT
    use_track_numbers: bool = False
    use_album_track_numbers: bool = False
    use_artist_subfolders: bool = False
    use_album_subfolders: bool = False
    create_playlist_subfolders: bool = True
    first_artist_only: bool = False
    artist_separator: str | None = None
    include_featuring: bool = True

    # ── Lyrics ──────────────────────────────────────────────────────────
    embed_lyrics: bool = True
    lyrics_providers: list[str] = field(
        default_factory=lambda: list(DEFAULT_LYRICS_PROVIDERS),
    )
    apple_lyrics_word_by_word: bool = True
    save_lrc: bool = False
    lrc_library_dir: str | None = None

    # ── Metadata enrichment ─────────────────────────────────────────────
    enrich_metadata: bool = True
    enrich_providers: list[str] = field(
        default_factory=lambda: list(DEFAULT_ENRICH_PROVIDERS),
    )

    # ── Reliability ─────────────────────────────────────────────────────
    track_max_retries: int = 0
    max_concurrent_downloads: int = 2
    timeout_s: int = 180
    resume: bool = True

    # ── After the run ───────────────────────────────────────────────────
    post_download_action: str = "none"
    post_download_command: str = ""
    post_download_hooks: list[str] = field(default_factory=list)

    # ── Custom endpoints ────────────────────────────────────────────────
    qobuz_local_api_url: str | None = None
    tidal_custom_api: str | None = None

    # ── Reporting and library ───────────────────────────────────────────
    # The wizard never asked about these; the launcher reads them all with a
    # default, and a loaded profile can carry them. Keeping them here is what
    # makes `to_cfg()` a complete answer rather than a partial one.
    json_report: bool = False
    verify_hires: bool = False
    write_m3u: str | None = None
    m3u_format: str = "m3u8"
    library_type: str | None = None
    library_url: str | None = None
    library_token: str | None = None
    library_user: str | None = None

    # ── Scheduling ──────────────────────────────────────────────────────
    loop: int | None = None
    watch: int | None = None

    # ── Diagnostics ─────────────────────────────────────────────────────
    # `verbose` and `log_level` are two ways of saying the same thing, and
    # the launcher reads both: verbose is the coarse switch a UI offers,
    # log_level the exact one a profile can carry.
    verbose: bool = False
    log_level: int | None = None

    #: Set when the state came from a saved profile, purely so the UI can say
    #: which one. Never reaches `to_cfg()`.
    profile_loaded: str | None = None

    # ------------------------------------------------------------------
    # Derived questions — what the UI should let the user touch
    # ------------------------------------------------------------------

    @property
    def is_playlist(self) -> bool:
        """Whether the target is a set of tracks, so playlist options apply."""
        return is_playlist_url(self.url)

    @property
    def uses_csv(self) -> bool:
        """A track list and a link are the two mutually exclusive inputs."""
        return bool(self.csv_path)

    @property
    def atmos_applies(self) -> bool:
        """Whether Dolby Atmos is worth offering at all.

        Only Tidal serves it. With Tidal among the providers the choice is
        real — Tidal gets Atmos, the others get their best lossless — and
        without it the option would be a lie.
        """
        return ATMOS_PROVIDER in self.services

    @property
    def bitrate_applies(self) -> bool:
        """Only a lossy target reads a bitrate; for the rest it is a no-op."""
        return bool(self.transcode_to) and not is_lossless(self.transcode_to)

    @property
    def separator_applies(self) -> bool:
        """A separator joins several artists, so one artist makes it moot."""
        return not self.first_artist_only

    @property
    def subfolders_apply(self) -> bool:
        """Track numbering and artist/album subfolders are alternatives.

        The wizard made this a fork — number the files *or* file them into
        folders — and the option it did not ask about it silently forced off.
        Stated once here, the UI can simply grey the losing branch out.
        """
        return not self.use_track_numbers

    def _requirements(self) -> list[tuple[bool, str, tuple[str, ...]]]:
        """Every requirement once: whether it is unmet, how to say so, and
        which controls it is about.

        Both `missing_requirements()` and `missing_fields()` read this, so
        the banner's wording and the markers on the form cannot drift apart
        — the panel never has to match on English to know what to flag.
        """
        return [
            (
                not self.url and not self.csv_path,
                "a URL or a CSV track list",
                ("url", "csv_path"),
            ),
            (not self.output_dir, "a destination folder", ("output_dir",)),
            (not self.services, "at least one download provider", ("services",)),
            (
                self.post_download_action == "command"
                and not self.post_download_command,
                "a command to run after the download",
                ("post_download_command",),
            ),
        ]

    def missing_requirements(self) -> list[str]:
        """What still stops this run from starting, in plain words.

        The wizard could not have this: it asked for each thing in turn and
        refused to move on. A screen where everything is editable needs to be
        able to say, at any moment, what is not yet answered.
        """
        return [phrase for unmet, phrase, _ in self._requirements() if unmet]

    def missing_fields(self) -> set[str]:
        """The same, as the field names of the controls to mark."""
        return {
            name
            for unmet, _, names in self._requirements()
            if unmet
            for name in names
        }

    @property
    def is_runnable(self) -> bool:
        return not self.missing_requirements()

    # ------------------------------------------------------------------
    # Coherence
    # ------------------------------------------------------------------

    def normalized(self) -> ConfigState:
        """A copy with every dependent setting settled.

        Same rules the wizard enforced by skipping questions, applied all at
        once instead of in order — which is what makes the state safe to edit
        in any sequence. Returns a new object rather than mutating, so a UI
        can show the normalised result while keeping what the user typed.
        """
        state = replace(
            self,
            services=list(self.services),
            lyrics_providers=list(self.lyrics_providers),
            enrich_providers=list(self.enrich_providers),
            post_download_hooks=list(self.post_download_hooks),
        )

        # A track list and a link are alternatives; the CSV wins, because
        # picking one is the more deliberate act of the two.
        if state.csv_path:
            state.url = ""

        state.quality = normalize_quality(state.quality)
        if state.quality == "DOLBY_ATMOS" and ATMOS_PROVIDER not in state.services:
            state.quality = "HI_RES_LOSSLESS"
        if state.quality not in QUALITY_TIERS:
            # HI_RES, HIGH and LOW can still arrive from a saved profile or
            # a hand-edited config; they map onto the tier that means the
            # same thing here rather than being offered back.
            state.quality = (
                "HI_RES_LOSSLESS" if state.quality == "HI_RES" else "LOSSLESS"
            )

        # Numbering the files and filing them into folders are the two ways
        # of organising a library, and the wizard never offered both.
        if state.use_track_numbers:
            state.use_artist_subfolders = False
            state.use_album_subfolders = False
            state.first_artist_only = False
        else:
            state.use_album_track_numbers = False

        if state.first_artist_only:
            state.artist_separator = None
        elif not state.artist_separator:
            # "" and None both mean "standard multi-value tags"; downstream
            # only understands the second spelling.
            state.artist_separator = None

        # Playlist subfolders are meaningless for a single track, and the
        # wizard left the default standing rather than asking.
        if not is_playlist_url(state.url):
            state.create_playlist_subfolders = True

        if not state.embed_lyrics:
            state.save_lrc = False
            state.lrc_library_dir = None
        if not state.lrc_library_dir:
            state.lrc_library_dir = None

        if not state.lyrics_providers:
            state.lyrics_providers = list(DEFAULT_LYRICS_PROVIDERS)
        if not state.enrich_providers:
            state.enrich_providers = list(DEFAULT_ENRICH_PROVIDERS)

        if state.post_download_action not in POST_DOWNLOAD_ACTIONS:
            state.post_download_action = "none"

        # Blank endpoints mean "not configured", which downstream spells None.
        state.output_path = state.output_path or None
        state.qobuz_local_api_url = state.qobuz_local_api_url or None
        state.tidal_custom_api = state.tidal_custom_api or None

        state.track_max_retries = max(0, int(state.track_max_retries or 0))
        state.max_concurrent_downloads = max(
            1,
            int(state.max_concurrent_downloads or 1),
        )
        state.timeout_s = max(0, int(state.timeout_s or 0))

        return state

    # ------------------------------------------------------------------
    # The contract
    # ------------------------------------------------------------------

    def to_cfg(self) -> dict[str, Any]:
        """The `cfg` dict `launcher.amain()` hands to `_run_download_async`.

        Deliberately exhaustive: every key the launcher reads is present,
        including the several the wizard never asked about and left to
        `cfg.get()` defaults. A partial dict would still run — and would
        quietly stop carrying whichever setting was added next.
        """
        state = self.normalized()
        return {
            "url": state.url,
            "csv_path": state.csv_path,
            "output_dir": state.output_dir,
            "output_path": state.output_path,
            "services": list(state.services),
            "quality": state.quality,
            "allow_fallback": state.allow_fallback,
            "transcode_to": state.transcode_to,
            "transcode_bitrate": state.transcode_bitrate,
            "transcode_keep_original": state.transcode_keep_original,
            "filename_format": state.filename_format,
            "use_track_numbers": state.use_track_numbers,
            "use_album_track_numbers": state.use_album_track_numbers,
            "use_artist_subfolders": state.use_artist_subfolders,
            "use_album_subfolders": state.use_album_subfolders,
            "create_playlist_subfolders": state.create_playlist_subfolders,
            "first_artist_only": state.first_artist_only,
            "artist_separator": state.artist_separator,
            "include_featuring": state.include_featuring,
            "embed_lyrics": state.embed_lyrics,
            "lyrics_providers": list(state.lyrics_providers),
            "apple_lyrics_word_by_word": state.apple_lyrics_word_by_word,
            "save_lrc": state.save_lrc,
            "lrc_library_dir": state.lrc_library_dir,
            "enrich_metadata": state.enrich_metadata,
            "enrich_providers": list(state.enrich_providers),
            "track_max_retries": state.track_max_retries,
            "max_concurrent_downloads": state.max_concurrent_downloads,
            "timeout_s": state.timeout_s,
            "resume": state.resume,
            "post_download_action": state.post_download_action,
            "post_download_command": state.post_download_command,
            "post_download_hooks": list(state.post_download_hooks),
            "qobuz_local_api_url": state.qobuz_local_api_url,
            "tidal_custom_api": state.tidal_custom_api,
            "json_report": state.json_report,
            "verify_hires": state.verify_hires,
            "write_m3u": state.write_m3u,
            "m3u_format": state.m3u_format,
            "library_type": state.library_type,
            "library_url": state.library_url,
            "library_token": state.library_token,
            "library_user": state.library_user,
            "loop": state.loop,
            "watch": state.watch,
            "verbose": state.verbose,
            "log_level": state.log_level,
        }

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> ConfigState:
        """Rebuilds a state from a `cfg` dict — a saved profile, usually.

        Unknown keys are dropped rather than raising: profiles outlive the
        settings that were in them, and one stale key should not make a
        profile unloadable.
        """
        known = {f.name for f in cls.__dataclass_fields__.values()}
        data = {key: value for key, value in cfg.items() if key in known}

        # Profiles written by the wizard use this spelling.
        if "_profile_loaded" in cfg:
            data["profile_loaded"] = cfg["_profile_loaded"]

        for key in ("services", "lyrics_providers", "enrich_providers",
                    "post_download_hooks"):
            if key in data and data[key] is not None:
                data[key] = list(data[key])

        for key in ("url", "csv_path", "output_dir", "filename_format",
                    "post_download_command", "transcode_bitrate", "m3u_format"):
            if data.get(key) is None:
                data.pop(key, None)

        return cls(**data)

    # ------------------------------------------------------------------
    # Presentation
    # ------------------------------------------------------------------

    def for_preview(self) -> ConfigState:
        """A copy with a visible placeholder wherever a requirement is unmet.

        Only the command preview uses this. A state with no URL and no
        providers renders as `spotiflac \'\' …  -s` — an empty quoted string
        and a flag with nothing after it, which reads like a bug in the
        generator rather than like a question still to answer. Naming the
        gap makes the preview usable as a template you fill in.
        """
        gaps: dict[str, object] = {}
        if not self.url and not self.csv_path:
            gaps["url"] = "<URL-or-CSV>"
        if not self.output_dir:
            gaps["output_dir"] = "<FOLDER>"
        if not self.services:
            gaps["services"] = ["<PROVIDER>"]
        if self.post_download_action == "command" and not self.post_download_command:
            gaps["post_download_command"] = "<COMMAND>"
        return replace(self, **gaps) if gaps else self

    def cli_command(self) -> str:
        """The equivalent `spotiflac ...` invocation, for the live panel.

        Free, because the wizard already had to build this to print at the
        end of a run; here it updates as the options change, which turns it
        from a parting note into a way of learning the CLI while using the
        UI.
        """
        return format_command(self.to_cfg())

    def log_level_name(self) -> str:
        if self.log_level is None:
            return "default"
        return logging.getLevelName(self.log_level)

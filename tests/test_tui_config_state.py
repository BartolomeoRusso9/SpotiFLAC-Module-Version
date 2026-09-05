"""`ConfigState.to_cfg()` against what the launcher actually reads.

The TUI replaces the wizard by producing the same dict, and the risk that
buys is drift: someone adds a download flag, wires it into argparse and into
`_run_download_async`, and the guided mode silently stops carrying it. Nobody
notices, because a missing key just means the `cfg.get()` default.

So the expected key set is not written down here — it is *read out of the
source* of the two modules that consume the dict, `launcher.py` and
`core/cli_preview.py`. Add a flag to either and this test tells you the TUI
has not learned about it yet.

The wizard it replaced is gone, and with it the parity check that compared
the two dicts key for key. What is left is the check that outlives it: the
expected key set is read out of the code that consumes the dict, so it cannot
drift from what a run actually needs.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from SpotiFLAC.tui.config_state import (
    DEFAULT_ENRICH_PROVIDERS,
    DEFAULT_LYRICS_PROVIDERS,
    ConfigState,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "SpotiFLAC"


def _cfg_keys_in(path: pathlib.Path, *, within: range | None = None) -> set[str]:
    """Every `cfg["x"]` and `cfg.get("x")` in a module, optionally line-bounded."""
    tree = ast.parse(path.read_text())
    keys: set[str] = set()

    for node in ast.walk(tree):
        if within is not None and getattr(node, "lineno", -1) not in within:
            continue
        # cfg["x"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "cfg"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
        # cfg.get("x") / cfg.get("x", default)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "cfg"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


def _run_download_from_cfg_lines() -> range:
    """The launcher function that turns a cfg into a run, found by name."""
    tree = ast.parse((_ROOT / "launcher.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "run_download_from_cfg"
        ):
            return range(node.lineno, (node.end_lineno or node.lineno) + 1)
    pytest.fail("launcher.run_download_from_cfg is gone — the contract moved")
    raise AssertionError  # unreachable, keeps type checkers happy


def _keys_the_run_consumes() -> set[str]:
    launcher_keys = _cfg_keys_in(
        _ROOT / "launcher.py",
        within=_run_download_from_cfg_lines(),
    )
    preview_keys = _cfg_keys_in(_ROOT / "core" / "cli_preview.py")
    # Read in the --interactive branch to pick the log level, and by the TUI
    # for the same reason; it never reaches _run_download_async as a kwarg.
    return launcher_keys | preview_keys | {"verbose"}


def test_to_cfg_covers_every_key_the_launcher_reads() -> None:
    produced = set(ConfigState().to_cfg())
    missing = _keys_the_run_consumes() - produced

    assert not missing, (
        "ConfigState.to_cfg() is missing keys that a download run reads: "
        f"{sorted(missing)}. Add the matching field to ConfigState — a flag "
        "wired into the CLI but not into the TUI is silently dropped."
    )


def test_to_cfg_produces_nothing_the_run_ignores() -> None:
    """The other direction: a key nobody reads is dead weight, or a typo."""
    produced = set(ConfigState().to_cfg())
    unread = produced - _keys_the_run_consumes()

    assert not unread, (
        f"ConfigState.to_cfg() emits keys nothing consumes: {sorted(unread)}. "
        "Either the reader was removed, or the key is misspelled."
    )


# ---------------------------------------------------------------------------
# The rules the wizard used to enforce by not asking
# ---------------------------------------------------------------------------


def test_track_numbers_win_over_subfolders() -> None:
    cfg = ConfigState(
        use_track_numbers=True,
        use_album_track_numbers=True,
        use_artist_subfolders=True,
        use_album_subfolders=True,
        first_artist_only=True,
    ).to_cfg()

    assert cfg["use_track_numbers"] is True
    assert cfg["use_album_track_numbers"] is True
    assert cfg["use_artist_subfolders"] is False
    assert cfg["use_album_subfolders"] is False
    assert cfg["first_artist_only"] is False


def test_album_track_numbers_need_track_numbers() -> None:
    cfg = ConfigState(use_track_numbers=False, use_album_track_numbers=True).to_cfg()
    assert cfg["use_album_track_numbers"] is False


def test_one_artist_leaves_no_separator_to_apply() -> None:
    state = ConfigState(first_artist_only=True, artist_separator=" / ")
    assert state.separator_applies is False
    assert state.to_cfg()["artist_separator"] is None


def test_a_blank_separator_means_standard_multi_value_tags() -> None:
    assert ConfigState(artist_separator="").to_cfg()["artist_separator"] is None


def test_turning_lyrics_off_clears_the_lrc_settings() -> None:
    cfg = ConfigState(
        embed_lyrics=False,
        save_lrc=True,
        lrc_library_dir="/tmp/lrc",
    ).to_cfg()

    assert cfg["save_lrc"] is False
    assert cfg["lrc_library_dir"] is None
    # The provider order survives, so turning lyrics back on restores it.
    assert cfg["lyrics_providers"] == list(DEFAULT_LYRICS_PROVIDERS)


def test_playlist_subfolders_only_apply_to_a_playlist() -> None:
    single = ConfigState(
        url="https://open.spotify.com/track/x",
        create_playlist_subfolders=False,
    )
    assert single.is_playlist is False
    assert single.to_cfg()["create_playlist_subfolders"] is True

    playlist = ConfigState(
        url="https://open.spotify.com/playlist/x",
        create_playlist_subfolders=False,
    )
    assert playlist.is_playlist is True
    assert playlist.to_cfg()["create_playlist_subfolders"] is False


def test_a_csv_replaces_the_url() -> None:
    state = ConfigState(url="https://open.spotify.com/track/x", csv_path="/tmp/list.csv")
    cfg = state.to_cfg()

    assert state.uses_csv is True
    assert cfg["csv_path"] == "/tmp/list.csv"
    assert cfg["url"] == ""


def test_a_bitrate_only_applies_to_a_lossy_target() -> None:
    assert ConfigState(transcode_to="mp3").bitrate_applies is True
    assert ConfigState(transcode_to="flac").bitrate_applies is False
    assert ConfigState(transcode_to=None).bitrate_applies is False


def test_empty_provider_lists_fall_back_to_the_defaults() -> None:
    cfg = ConfigState(lyrics_providers=[], enrich_providers=[]).to_cfg()
    assert cfg["lyrics_providers"] == list(DEFAULT_LYRICS_PROVIDERS)
    assert cfg["enrich_providers"] == list(DEFAULT_ENRICH_PROVIDERS)


def test_numeric_settings_are_clamped_the_way_the_wizard_clamped_them() -> None:
    cfg = ConfigState(
        track_max_retries=-5,
        max_concurrent_downloads=0,
        timeout_s=-1,
    ).to_cfg()

    assert cfg["track_max_retries"] == 0
    assert cfg["max_concurrent_downloads"] == 1
    assert cfg["timeout_s"] == 0


def test_blank_endpoints_become_none() -> None:
    cfg = ConfigState(
        output_path="",
        qobuz_local_api_url="",
        tidal_custom_api="",
    ).to_cfg()

    assert cfg["output_path"] is None
    assert cfg["qobuz_local_api_url"] is None
    assert cfg["tidal_custom_api"] is None


def test_an_unknown_post_download_action_falls_back_to_none() -> None:
    assert ConfigState(post_download_action="rm -rf").to_cfg()["post_download_action"] == "none"


def test_normalizing_does_not_mutate_the_original() -> None:
    state = ConfigState(use_track_numbers=True, use_album_subfolders=True)
    normalized = state.normalized()

    assert normalized.use_album_subfolders is False
    assert state.use_album_subfolders is True, "normalized() must return a copy"


def test_quality_is_normalized() -> None:
    assert ConfigState(quality="lossless").to_cfg()["quality"] == "LOSSLESS"


# ---------------------------------------------------------------------------
# Readiness, profiles, presentation
# ---------------------------------------------------------------------------


def test_a_fresh_state_says_exactly_what_it_is_missing() -> None:
    missing = ConfigState().missing_requirements()
    assert missing == [
        "a URL or a CSV track list",
        "a destination folder",
        "at least one download provider",
    ]


def test_a_command_action_needs_a_command() -> None:
    state = ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/o",
        services=["tidal"],
        post_download_action="command",
    )
    assert state.is_runnable is False
    assert "a command to run after the download" in state.missing_requirements()

    state.post_download_command = "echo done"
    assert state.is_runnable is True


def test_a_csv_alone_is_enough_to_run() -> None:
    state = ConfigState(csv_path="/tmp/l.csv", output_dir="/tmp/o", services=["tidal"])
    assert state.is_runnable is True


def test_from_cfg_round_trips_through_to_cfg() -> None:
    original = ConfigState(
        url="https://open.spotify.com/playlist/x",
        output_dir="/tmp/o",
        services=["tidal", "qobuz"],
        quality="HI_RES",
        transcode_to="mp3",
        transcode_bitrate="192k",
        use_artist_subfolders=True,
        lyrics_providers=["lrclib"],
        enrich_providers=["deezer"],
        track_max_retries=3,
        timeout_s=90,
        post_download_action="notify",
        loop=30,
    )

    assert ConfigState.from_cfg(original.to_cfg()).to_cfg() == original.to_cfg()


def test_from_cfg_ignores_keys_it_does_not_know() -> None:
    state = ConfigState.from_cfg(
        {"output_dir": "/tmp/o", "a_setting_from_2019": True, "_profile_loaded": "mine"},
    )
    assert state.output_dir == "/tmp/o"
    assert state.profile_loaded == "mine"


def test_profile_loaded_never_reaches_the_run() -> None:
    assert "profile_loaded" not in ConfigState(profile_loaded="mine").to_cfg()
    assert "_profile_loaded" not in ConfigState(profile_loaded="mine").to_cfg()


def test_the_cli_preview_reflects_the_state() -> None:
    state = ConfigState(
        url="https://open.spotify.com/track/x",
        output_dir="/tmp/o",
        services=["tidal"],
        first_artist_only=True,
    )
    command = state.cli_command()

    assert command.startswith("spotiflac")
    assert "--first-artist-only" in command

    state.first_artist_only = False
    assert "--first-artist-only" not in state.cli_command()


def test_the_cli_preview_uses_csv_when_there_is_one() -> None:
    state = ConfigState(csv_path="/tmp/l.csv", output_dir="/tmp/o", services=["tidal"])
    assert "--csv" in state.cli_command()

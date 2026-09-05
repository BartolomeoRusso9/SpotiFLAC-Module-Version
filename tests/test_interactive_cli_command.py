"""The "equivalent CLI command" builder.

It began as `interactive._print_cli_command`, shown at the end of the wizard;
it now lives in `core/cli_preview.py`, where the TUI renders it as a panel
that updates while the options change and the wizard still prints it. This
tests the builder rather than either frontend's way of showing it, so it says
nothing about how a run was configured — only about what the configuration
would look like as a command.
"""

from __future__ import annotations

from SpotiFLAC.core.cli_preview import build_command_parts, format_command

_BASE_CFG = {
    "url": "https://open.spotify.com/track/abc123",
    "output_dir": "./Downloads",
    "services": ["ext:tidal-web"],
    "quality": "LOSSLESS",
    "filename_format": "{title} - {artist}",
    "use_track_numbers": False,
    "use_album_track_numbers": False,
    "use_artist_subfolders": False,
    "use_album_subfolders": False,
    "first_artist_only": False,
    "embed_lyrics": True,
    "lyrics_providers": ["apple", "lrclib"],
    "enrich_metadata": True,
    "enrich_providers": ["deezer"],
}


def test_watch_is_omitted_when_not_set() -> None:
    assert "--watch" not in build_command_parts(dict(_BASE_CFG))


def test_watch_flag_is_included_when_set() -> None:
    parts = build_command_parts(dict(_BASE_CFG, watch=60))
    # A list, so this can check adjacency rather than mere presence — the
    # value has to belong to the flag it follows.
    assert parts[parts.index("--watch") + 1] == "60"


def test_watch_and_loop_can_both_appear() -> None:
    parts = build_command_parts(dict(_BASE_CFG, watch=1440, loop=30))
    assert parts[parts.index("--loop") + 1] == "30"
    assert parts[parts.index("--watch") + 1] == "1440"


def test_a_flag_keeps_its_values_on_one_line() -> None:
    """The command is read, not just pasted.

    One token per line — which is what this used to do — turns
    `--lyrics-providers apple lrclib` into three lines that each say nothing.
    Grouping is the difference between a command you can scan and a column of
    words.
    """
    rendered = format_command(dict(_BASE_CFG))
    lines = [line.strip().rstrip(" \\") for line in rendered.split("\n")]

    assert "-s ext:tidal-web" in lines
    assert "--lyrics-providers apple lrclib" in lines
    assert lines[0].startswith("spotiflac https://open.spotify.com/track/abc123")


def test_the_rendered_command_still_parses_as_a_shell_line() -> None:
    """Readable is worth nothing if it no longer runs."""
    import shlex

    rendered = format_command(dict(_BASE_CFG, post_download_command="echo 'a b'"))
    assert shlex.split(rendered.replace("\\\n", " ")) == build_command_parts(
        dict(_BASE_CFG, post_download_command="echo 'a b'"),
    )


def test_the_wizard_still_prints_what_the_builder_produces(capsys) -> None:
    """One release left: the wizard's printer must stay in step."""
    from SpotiFLAC import interactive

    cfg = dict(_BASE_CFG, watch=60)
    interactive._print_cli_command(cfg)
    assert format_command(cfg) in capsys.readouterr().out

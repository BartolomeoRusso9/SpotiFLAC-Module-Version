"""Tests for interactive.py's _print_cli_command() — the "equivalent CLI
command" generator shown at the end of the wizard. Pure function (reads
`cfg`, prints), so this only exercises the --watch addition without
driving the whole input()-based wizard flow.
"""

from __future__ import annotations

from SpotiFLAC import interactive

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


def test_watch_is_omitted_when_not_set(capsys) -> None:
    interactive._print_cli_command(dict(_BASE_CFG))
    out = capsys.readouterr().out
    assert "--watch" not in out


def test_watch_flag_is_included_when_set(capsys) -> None:
    cfg = dict(_BASE_CFG, watch=60)
    interactive._print_cli_command(cfg)
    out = capsys.readouterr().out
    # Parts are joined one-per-line (" \\\n    "), so check presence rather
    # than exact adjacency.
    assert "--watch" in out
    assert "60" in out


def test_watch_and_loop_can_both_appear(capsys) -> None:
    cfg = dict(_BASE_CFG, watch=1440, loop=30)
    interactive._print_cli_command(cfg)
    out = capsys.readouterr().out
    assert "--loop" in out and "30" in out
    assert "--watch" in out and "1440" in out

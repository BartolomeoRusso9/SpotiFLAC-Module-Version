"""Getting a path into NODE_OPTIONS intact.

The host preloads two guards into the extension's Node process with
`--require`, passed through NODE_OPTIONS. Node parses that variable itself,
and not the way a shell would: it splits on whitespace — so an unquoted path
containing a space arrives as two arguments and Node exits on the second —
but *inside* the quotes it also treats a backslash as an escape character.

Quoting alone therefore fixed spaces and broke Windows. Every separator was
eaten on the way in, so

    C:\\Users\\Bartolomeo\\...\\SpotiFLAC\\extensions\\_netguard.js

reached Node as `C:UsersBartolomeo..._netguard.js`, and the bridge died at
startup with MODULE_NOT_FOUND before the extension was ever read — reported
to the user as an opaque "Extension did not respond within 30.0s".

The round-trip tests below run the real Node, because the rule being relied
on is Node's, not one this repo gets to define.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from SpotiFLAC.extensions.runtime import quote_node_option

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node to check its own parsing"
)


def _node_with(node_options: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    env["NODE_OPTIONS"] = node_options
    return subprocess.run(
        ["node", "-e", "0"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def test_backslashes_are_doubled_and_posix_paths_are_untouched() -> None:
    assert (
        quote_node_option(r"C:\Users\B\SpotiFLAC\extensions\_netguard.js")
        == r'"C:\\Users\\B\\SpotiFLAC\\extensions\\_netguard.js"'
    )
    assert quote_node_option("/opt/a b/_netguard.js") == '"/opt/a b/_netguard.js"'


def _preload_reaches_node(tmp_path: Path, directory: str) -> bool:
    """Puts a preload script in `directory` and reports whether Node ran it."""
    target = tmp_path / directory
    target.mkdir(parents=True, exist_ok=True)
    preload = target / "_netguard.js"
    preload.write_text("console.error('PRELOAD OK');")

    result = _node_with(f"--require {quote_node_option(preload)}")
    return "PRELOAD OK" in result.stderr


@needs_node
def test_a_path_with_spaces_still_loads(tmp_path: Path) -> None:
    assert _preload_reaches_node(tmp_path, "Program Files/SpotiFLAC")


@needs_node
@pytest.mark.skipif(sys.platform != "win32", reason="only Windows has backslash paths")
def test_a_windows_path_loads(tmp_path: Path) -> None:  # pragma: no cover
    assert _preload_reaches_node(tmp_path, "Users/Bartolomeo/SpotiFLAC/extensions")


@needs_node
def test_node_really_does_unescape_backslashes_inside_the_quotes() -> None:
    """The premise the quoting exists for, asserted rather than assumed.

    Runs everywhere: it never touches the filesystem, it just reads back the
    path Node says it could not find.
    """
    # One backslash in, none out — this is the bug, straight from Node.
    assert "/nope/abc.js" in _node_with('--require "/nope/a\\b\\c.js"').stderr

    # Doubled by quote_node_option, so Node hands the separators back.
    windows_ish = "/nope/a\\b\\c.js"
    kept = _node_with("--require " + quote_node_option(windows_ish))
    assert windows_ish in kept.stderr

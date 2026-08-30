"""Extensions must not be able to write wherever they like.

Extensions are third-party JavaScript running with the user's own
permissions. Nothing stopped one writing to a shell profile, to
~/.ssh/authorized_keys, or — most pointedly — to
~/.spotiflac/trusted_keys.json, the Ed25519 root of trust that decides
which registry entries count as signed. An extension that can add a key
there can sign its own successors, defeating the signing scheme entirely.

A provider needs three places: the file it was asked to produce, its own
directory, and a temp directory. These tests pin that boundary down with
the real preload under the real Node, and cover two details that a
plausible-looking implementation gets wrong:

  - macOS resolves os.tmpdir() to /private/var/..., so an allow-list that
    is not itself realpath-ed rejects the very temp directory it permits;
  - the extension runs inside a worker_threads Worker, not the main
    thread, so a preload that did not reach workers would protect nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

EXTENSIONS = Path(__file__).resolve().parents[1] / "SpotiFLAC" / "extensions"
FSGUARD = EXTENSIONS / "_fsguard.js"
NETGUARD = EXTENSIONS / "_netguard.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node to exercise the real preload"
)

PROBE = r"""
const fs = require('fs');
const target = process.argv[1];
try {
  fs.writeFileSync(target, 'x');
  fs.unlinkSync(target);
  console.log('written');
} catch (e) {
  console.log(String(e.message).includes('fsguard') ? 'blocked' : 'error:' + e.code);
}
"""


def _write(
    target: Path, *, cwd: Path, tmpdir: Path, env=None, script: str = PROBE
) -> str:
    """Runs the probe with TMPDIR pointed somewhere we control.

    pytest's own tmp_path lives *under* the system temp directory, which the
    guard allows by design — so without this every "an unrelated directory
    is refused" case would be testing a directory the guard legitimately
    permits, and would fail while the guard was working correctly.
    """
    out = subprocess.run(
        ["node", "--require", str(FSGUARD), "-e", script, str(target)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={
            **os.environ,
            "TMPDIR": str(tmpdir),
            "TMP": str(tmpdir),
            "TEMP": str(tmpdir),
            **(env or {}),
        },
        timeout=30,
    )
    return out.stdout.strip()


@pytest.fixture
def extdir(tmp_path) -> Path:
    d = tmp_path / "extension"
    d.mkdir()
    return d


@pytest.fixture
def scratch(tmp_path) -> Path:
    """The only temp directory the guard under test will know about."""
    d = tmp_path / "scratch"
    d.mkdir()
    return d


# --- what must be refused --------------------------------------------------


def test_the_trust_store_cannot_be_written(extdir, scratch) -> None:
    """The sharpest case: writing here lets an extension sign its own
    successors.
    """
    target = Path.home() / ".spotiflac" / "trusted_keys.json"
    assert _write(target, cwd=extdir, tmpdir=scratch) == "blocked"


@pytest.mark.parametrize(
    "relative",
    [".ssh/authorized_keys", ".bashrc", ".zshrc", ".config/spotiflac_probe"],
)
def test_the_home_directory_is_off_limits(extdir, scratch, relative) -> None:
    assert _write(Path.home() / relative, cwd=extdir, tmpdir=scratch) == "blocked"


def test_an_unrelated_directory_is_off_limits(extdir, scratch, tmp_path) -> None:
    other = tmp_path / "somewhere-else"
    other.mkdir()
    assert _write(other / "x.flac", cwd=extdir, tmpdir=scratch) == "blocked"


# --- what must be permitted ------------------------------------------------


def test_the_extension_can_write_in_its_own_directory(extdir, scratch) -> None:
    assert _write(extdir / "cache.json", cwd=extdir, tmpdir=scratch) == "written"


def test_the_temp_directory_is_permitted(extdir, scratch) -> None:
    """On macOS os.tmpdir() is /var/folders/... and the filesystem answers
    /private/var/folders/..., so an allow-list that is not realpath-ed
    rejects the directory it means to allow. It did, before this was fixed.
    """
    assert _write(scratch / "probe.tmp", cwd=extdir, tmpdir=scratch) == "written"


def test_configured_directories_are_permitted(extdir, scratch, tmp_path) -> None:
    downloads = tmp_path / "Music"
    downloads.mkdir()
    result = _write(
        downloads / "x.flac",
        cwd=extdir,
        tmpdir=scratch,
        env={"SPOTIFLAC_EXT_WRITABLE_DIRS": str(downloads)},
    )
    assert result == "written"


def test_the_host_can_sanction_a_path_at_run_time(extdir, scratch, tmp_path) -> None:
    """The output directory is chosen per download, so the bridge registers
    it as it dispatches the call — a static list could not cover it.
    """
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "song.flac"

    assert _write(target, cwd=extdir, tmpdir=scratch) == "blocked"

    sanctioning = textwrap.dedent(
        """
        const fs = require('fs');
        global.__spotiflacAllowWrite(process.argv[1]);
        try { fs.writeFileSync(process.argv[1], 'x'); console.log('written'); }
        catch (e) { console.log('blocked'); }
        """
    )
    assert _write(target, cwd=extdir, tmpdir=scratch, script=sanctioning) == "written"


def test_the_opt_out_disables_the_guard(extdir, scratch, tmp_path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = _write(
        other / "x",
        cwd=extdir,
        tmpdir=scratch,
        env={"SPOTIFLAC_EXT_ALLOW_ANY_WRITE": "1"},
    )
    assert result == "written"


# --- the guards have to reach where the extension actually runs ------------


@pytest.mark.parametrize("guard", [FSGUARD, NETGUARD])
def test_a_preload_reaches_inside_a_worker_thread(guard, tmp_path) -> None:
    """Extensions execute in a worker_threads Worker (see _bridge.js), not
    the main thread. A preload that stopped at the main thread would guard
    an empty room — and would look correct in every direct test.
    """
    worker = textwrap.dedent(
        """
        const { Worker } = require('worker_threads');
        new Worker(`
          const net = require('net');
          const s = new net.Socket();
          s.on('error', (e) => {
            console.log(String(e.message).includes('netguard') ? 'guarded' : 'open');
            process.exit(0);
          });
          s.on('connect', () => { console.log('open'); process.exit(0); });
          s.connect(9, '127.0.0.1');
        `, { eval: true });
        """
    )
    out = subprocess.run(
        ["node", "--require", str(NETGUARD), "-e", worker],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.stdout.strip() == "guarded"


def test_both_guards_are_injected_by_the_runtime() -> None:
    runtime = (EXTENSIONS / "runtime.py").read_text()
    assert "_netguard.js" in runtime and "_fsguard.js" in runtime


def test_the_bridge_sanctions_the_download_path() -> None:
    """Without this the guard would refuse the one write the extension
    exists to perform.
    """
    bridge = (EXTENSIONS / "_bridge.js").read_text()
    assert "__spotiflacAllowWrite" in bridge


def test_the_opt_outs_survive_the_environment_sandbox() -> None:
    from SpotiFLAC.extensions.sandbox import build_env

    env = build_env(
        {
            "SPOTIFLAC_EXT_ALLOW_ANY_WRITE": "1",
            "SPOTIFLAC_EXT_WRITABLE_DIRS": "/tmp/x",
        }
    )
    assert env.get("SPOTIFLAC_EXT_ALLOW_ANY_WRITE") == "1"
    assert env.get("SPOTIFLAC_EXT_WRITABLE_DIRS") == "/tmp/x"


def test_the_probe_itself_is_not_vacuous(extdir, tmp_path) -> None:
    """A guard test that would pass with no guard at all proves nothing."""
    other = tmp_path / "unguarded"
    other.mkdir()
    out = subprocess.run(
        ["node", "-e", PROBE, str(other / "x")],
        capture_output=True,
        text=True,
        cwd=str(extdir),
        timeout=30,
    )
    assert out.stdout.strip() == "written"

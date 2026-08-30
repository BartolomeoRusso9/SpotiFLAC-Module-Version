"""Extensions must not be able to reach this machine's own network.

Extensions are third-party JavaScript from registries, running as an
ordinary Node subprocess with an ordinary network stack — so without a
guard one can reach SpotiFLAC's own `--web` server on 127.0.0.1, the LAN,
or 169.254.169.254 for cloud credentials in the Docker image the project
ships. None of that is anything a music provider needs.

These tests run the real preload under the real Node, because the failure
they cover is subtle and was live in the first version of the guard: Node
normalises Socket.connect()'s arguments into an array, so reading
args[0].host finds undefined and every literal IP address walks straight
through, while the DNS-based half of the check still works and makes it
look like the guard is doing its job.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = (
    Path(__file__).resolve().parents[1] / "SpotiFLAC" / "extensions" / "_netguard.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node to exercise the real preload"
)

#: A connect() attempt reports which of the two halves stopped it, so a test
#: can tell "blocked by the guard" from "nothing was listening there".
PROBE = r"""
const net = require('net');
const target = JSON.parse(process.argv[1]);
const sock = new net.Socket();
let done = false;
function say(v) { if (!done) { done = true; console.log(v); process.exit(0); } }
sock.on('error', (e) => say(String(e.message).includes('netguard') ? 'blocked' : 'reached:' + e.code));
sock.on('connect', () => { sock.destroy(); say('reached:ok'); });
setTimeout(() => { sock.destroy(); say('reached:timeout'); }, 2500);
sock.connect(target.port, target.host);
"""


def _attempt(host: str, port: int = 9, *, guard: bool = True, env=None) -> str:
    cmd = ["node"]
    if guard:
        cmd += ["--require", str(GUARD)]
    cmd += ["-e", PROBE, json.dumps({"host": host, "port": port})]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    return out.stdout.strip()


@pytest.mark.parametrize(
    ("host", "what"),
    [
        ("127.0.0.1", "loopback — SpotiFLAC's own --web server lives here"),
        ("169.254.169.254", "cloud instance metadata"),
        ("192.168.1.1", "a home router"),
        ("10.0.0.1", "RFC1918"),
        ("172.16.0.1", "RFC1918, the range that is easy to get wrong"),
        ("::1", "loopback over IPv6"),
        ("::ffff:127.0.0.1", "loopback wearing an IPv6 coat"),
    ],
)
def test_private_addresses_are_refused(host, what) -> None:
    assert _attempt(host) == "blocked", what


def test_a_hostname_resolving_to_loopback_is_refused() -> None:
    """Checking the URL's address alone is not enough: a name that resolves
    to 127.0.0.1 is the obvious way past it.
    """
    assert _attempt("localtest.me", 80) == "blocked"


def test_the_public_internet_still_works() -> None:
    """The guard has to be invisible to every extension doing its job."""
    assert _attempt("example.com", 80).startswith("reached")


def test_a_literal_ip_is_checked_at_the_socket(monkeypatch) -> None:
    """The regression that was live in the first version of the guard.

    A literal IP never reaches dns.lookup, so it is only ever caught by the
    socket-level check — and with that check silently broken the DNS half
    still passed, which is what made it look correct.
    """
    assert _attempt("127.0.0.1") == "blocked"
    assert _attempt("127.0.0.1", guard=False).startswith("reached")


def test_the_opt_out_lets_a_self_hosted_service_through() -> None:
    """Someone pointing an extension at a local Qobuz mirror needs a way to
    say so.
    """
    import os

    env = {**os.environ, "SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK": "1"}
    assert _attempt("127.0.0.1", env=env).startswith("reached")


def test_the_guard_is_injected_into_the_runtime() -> None:
    """A guard nothing loads protects nothing."""
    runtime = (GUARD.parent / "runtime.py").read_text()
    assert "_netguard.js" in runtime
    assert "--require" in runtime


def test_the_opt_out_survives_the_environment_sandbox() -> None:
    """build_env() strips everything not on its allowlist, so the opt-out
    has to be on it or it can never reach the extension.
    """
    from SpotiFLAC.extensions.sandbox import build_env

    env = build_env({"SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK": "1"})
    assert env.get("SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK") == "1"

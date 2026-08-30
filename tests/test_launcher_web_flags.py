"""`spotiflac --web` argument parsing.

The --web branch in launcher.amain() builds its own small parser for
--host/--port/--web-token/--web-multiuser. argparse expands unambiguous
option prefixes by default, and `--web` — always present, since it is what
selects this branch — is a prefix of both --web-token and --web-multiuser.
That makes it "ambiguous", and argparse's response to an ambiguous option
is to print usage and exit(2): the plain, documented `spotiflac --web`
invocation never reached the server at all.

These tests drive the real branch rather than a copy of the parser, so they
still hold if the option list grows another --web-something flag.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
webapp = pytest.importorskip("SpotiFLAC.webapp")

from SpotiFLAC import launcher


@pytest.fixture
def run_web_calls(monkeypatch):
    """Runs launcher.amain() with the given argv, capturing run_async kwargs
    instead of actually binding a socket.
    """
    calls: list[dict] = []

    async def _fake_run_async(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(webapp, "run_async", _fake_run_async)

    # Everything amain() does before the --web branch is network/console
    # noise for this test: the update check, the banner, the extension
    # bootstrap. Silence them so the test stays about argument parsing.
    monkeypatch.setattr(launcher, "_print_welcome_banner", lambda *a, **k: None)

    async def _no_updates():
        return None

    monkeypatch.setattr(launcher, "check_for_updates_async", _no_updates)

    def _run(argv: list[str]) -> dict:
        monkeypatch.setattr("sys.argv", ["spotiflac", *argv])
        asyncio.run(launcher.amain())
        assert calls, f"--web branch never reached run_async for {argv}"
        return calls[-1]

    return _run


def test_plain_web_flag_starts_the_server_on_the_documented_defaults(
    run_web_calls,
) -> None:
    kwargs = run_web_calls(["--web"])
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8000
    assert kwargs["token"] is None
    assert kwargs["multiuser"] is False


def test_web_accepts_host_and_port(run_web_calls) -> None:
    kwargs = run_web_calls(["--web", "--host", "0.0.0.0", "--port", "9000"])
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9000


def test_web_token_and_multiuser_still_parse(run_web_calls) -> None:
    kwargs = run_web_calls(["--web", "--web-token", "s3cret", "--web-multiuser"])
    assert kwargs["token"] == "s3cret"
    assert kwargs["multiuser"] is True


# ── --json and stdout ──────────────────────────────────────────────────────


def test_the_welcome_banner_is_suppressed_under_json(capsys) -> None:
    """--json puts the report on stdout, so nothing else may go there.
    Console output already went to stderr; the ASCII banner did not, which
    made `spotiflac ... --json | jq` fail on the first byte.
    """
    import sys

    from SpotiFLAC.launcher import _print_welcome_banner

    original = sys.argv
    try:
        sys.argv = ["spotiflac", "--json", "https://example.invalid", "/tmp"]
        _print_welcome_banner()
        assert capsys.readouterr().out == ""

        sys.argv = ["spotiflac", "https://example.invalid", "/tmp"]
        _print_welcome_banner()
        assert "SpotiFLAC" in capsys.readouterr().out
    finally:
        sys.argv = original


# --- --help must not reach the network -------------------------------------


def test_help_skips_the_startup_network_work() -> None:
    """argparse handles -h/--help further down amain(), and everything before
    that point — the update check and the extension registry bootstrap —
    reaches the network. `spotiflac --help` therefore waited on three HTTP
    attempts per configured registry to print a static string, and appeared
    to hang outright when a registry was slow to answer.
    """
    from SpotiFLAC.launcher import _is_help_invocation

    assert _is_help_invocation(["--help"])
    assert _is_help_invocation(["-h"])
    assert _is_help_invocation(["--gui", "--help"])


def test_a_real_command_is_not_mistaken_for_help() -> None:
    """The bootstrap has to keep running for everything that uses
    extensions, which is nearly everything.
    """
    from SpotiFLAC.launcher import _is_help_invocation

    for argv in (
        [],
        ["--gui"],
        ["--web"],
        ["https://open.spotify.com/track/x"],
        ["--output", "help"],
        ["--search", "-h-h"],
    ):
        assert not _is_help_invocation(argv), argv


def test_amain_guards_both_network_steps(monkeypatch, capsys) -> None:
    """Guarding only one of them would leave the other on the --help path.

    Run rather than read: an AST scan proves the two names appear under some
    `if not help_only`, not that neither actually runs. A guard that was
    correct in the source and wrong in effect — an early call added above
    it, a condition inverted — reads exactly the same to the parser.
    """
    import asyncio
    import sys

    from SpotiFLAC import launcher
    from SpotiFLAC.extensions import manager as ext_manager

    ran: list[str] = []

    async def _updates():
        ran.append("check_for_updates_async")

    def _manager(*args, **kwargs):
        ran.append("ExtensionManager")
        raise AssertionError("must not construct an ExtensionManager for --help")

    monkeypatch.setattr(launcher, "check_for_updates_async", _updates)
    monkeypatch.setattr(ext_manager, "ExtensionManager", _manager)
    # amain() takes no arguments: it reads sys.argv itself, both for the
    # help sniff above and for the parser below it.
    monkeypatch.setattr(sys, "argv", ["spotiflac", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(launcher.amain())

    assert exit_info.value.code == 0
    assert ran == [], f"--help still did network work: {ran}"
    assert "usage:" in capsys.readouterr().out

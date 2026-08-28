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

from SpotiFLAC import launcher  # noqa: E402


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

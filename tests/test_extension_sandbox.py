"""What a JavaScript extension's process inherits.

The node child was started with `os.environ.copy()`, so every third-party
extension read the host's whole environment out of `process.env` — tokens
included. These cover the allowlist that replaced it.

The limits of this are as important as the behaviour: it is not a security
boundary, and the module says so. It removes an accidental handover of
credentials; it does not contain a hostile extension.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from SpotiFLAC.extensions import sandbox


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (sandbox.DISABLE_ENV, sandbox.PASSTHROUGH_ENV):
        monkeypatch.delenv(name, raising=False)


# ── the allowlist ──────────────────────────────────────────────────────────


def test_secrets_in_the_host_environment_are_not_handed_over(monkeypatch) -> None:
    for name in (
        "SPOTIFLAC_WEB_TOKEN",
        "SPOTIFLAC_LIBRARY_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, "super-secret")

    env = sandbox.build_env()

    assert not any(name.startswith("AWS_") for name in env)
    assert "SPOTIFLAC_WEB_TOKEN" not in env
    assert "SPOTIFLAC_LIBRARY_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "super-secret" not in "".join(env.values())


def test_what_a_network_client_actually_needs_is_kept(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/corp.pem")

    env = sandbox.build_env()

    assert env["PATH"] == "/usr/bin"
    assert env["HTTPS_PROXY"] == "http://proxy.local:3128"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/corp.pem"


def test_absent_variables_are_simply_absent(monkeypatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    assert "HTTPS_PROXY" not in sandbox.build_env()


def test_extra_values_from_the_caller_are_merged_last() -> None:
    env = sandbox.build_env({"NODE_OPTIONS": "--openssl-legacy-provider"})
    assert env["NODE_OPTIONS"] == "--openssl-legacy-provider"


def test_an_extension_that_needs_a_variable_can_be_given_it(monkeypatch) -> None:
    """Opt-in and per-name: the operator says exactly what to hand over,
    rather than choosing between "everything" and "nothing".
    """
    monkeypatch.setenv("MY_PROVIDER_KEY", "abc123")
    assert "MY_PROVIDER_KEY" not in sandbox.build_env()

    monkeypatch.setenv(sandbox.PASSTHROUGH_ENV, "MY_PROVIDER_KEY , OTHER")
    env = sandbox.build_env()
    assert env["MY_PROVIDER_KEY"] == "abc123"


def test_the_sandbox_can_be_switched_off(monkeypatch) -> None:
    """For anyone whose extension broke and who would rather have the old
    behaviour than debug it right now.
    """
    monkeypatch.setenv("SPOTIFLAC_WEB_TOKEN", "secret")
    monkeypatch.setenv(sandbox.DISABLE_ENV, "1")

    assert sandbox.build_env()["SPOTIFLAC_WEB_TOKEN"] == "secret"
    assert "disabled" in sandbox.describe()


def test_dropped_names_reports_what_is_withheld(monkeypatch) -> None:
    monkeypatch.setenv("SOMETHING_PRIVATE", "x")
    assert "SOMETHING_PRIVATE" in sandbox.dropped_names()


def test_describe_says_how_much_was_withheld(monkeypatch) -> None:
    monkeypatch.setenv("SOMETHING_PRIVATE", "x")
    assert "withheld" in sandbox.describe()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only variables")
def test_windows_keeps_the_variables_the_os_needs() -> None:  # pragma: no cover
    env = sandbox.build_env()
    assert "SYSTEMROOT" in env


# ── resource limits ────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimits")
def test_a_preexec_hook_is_provided_on_posix() -> None:
    assert callable(sandbox.build_preexec())


def _in_subprocess(body: str) -> int:
    """Runs `body` in a fresh interpreter and returns its exit code.

    A subprocess rather than os.fork(): the loop_runner thread makes the
    test process multi-threaded, and forking one of those is a
    DeprecationWarning in 3.12+ (and a real deadlock risk). rlimits are
    per-process, so they have to be applied somewhere disposable either way.
    """
    import subprocess
    import sys
    import textwrap

    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        capture_output=True,
    ).returncode


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimits")
def test_the_hook_applies_limits_without_raising_them() -> None:
    """Asking for more than the current soft limit must never *raise* it —
    that would be a sandbox that hands out privilege.
    """
    assert _in_subprocess("""
            import resource, sys
            from SpotiFLAC.extensions.sandbox import build_preexec

            before, _ = resource.getrlimit(resource.RLIMIT_CPU)
            build_preexec(cpu_seconds=10**9)()
            after, _ = resource.getrlimit(resource.RLIMIT_CPU)

            if before != resource.RLIM_INFINITY and after > before:
                sys.exit(1)
            """) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimits")
def test_the_hook_is_safe_to_call_twice() -> None:
    assert _in_subprocess("""
            from SpotiFLAC.extensions.sandbox import build_preexec

            hook = build_preexec(memory_mb=512)
            hook()
            hook()
            """) == 0

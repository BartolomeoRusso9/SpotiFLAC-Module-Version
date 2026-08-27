"""Tests for core/node_check.py — the Node.js availability check and
best-effort auto-install (mirrors core/ffmpeg_check.py's check/print shape,
plus ensure_node_installed() for the README's documented auto-install
behavior). Every subprocess call is mocked: this suite must never actually
invoke a real package manager or modify the machine it runs on.
"""

from __future__ import annotations

import subprocess


from SpotiFLAC.core import node_check


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# check_node()
# ---------------------------------------------------------------------------


def test_check_node_reports_available(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        assert argv == ["node", "-v"]
        return _FakeCompletedProcess(0, stdout="v20.11.0\n")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    result = node_check.check_node()
    assert result["available"] is True
    assert result["version"] == "v20.11.0"
    assert result["error"] == ""


def test_check_node_reports_missing_binary(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    result = node_check.check_node()
    assert result["available"] is False
    assert "not found" in result["error"]


def test_check_node_reports_timeout(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    result = node_check.check_node()
    assert result["available"] is False
    assert "timed out" in result["error"]


def test_check_node_reports_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(
        node_check.subprocess, "run", lambda argv, **kw: _FakeCompletedProcess(1)
    )
    result = node_check.check_node()
    assert result["available"] is False


# ---------------------------------------------------------------------------
# _package_manager_command()
# ---------------------------------------------------------------------------


def test_package_manager_picks_first_available_on_linux(monkeypatch) -> None:
    monkeypatch.setattr(node_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        node_check.shutil,
        "which",
        lambda name: "/usr/bin/dnf" if name == "dnf" else None,
    )
    picked = node_check._package_manager_command()
    assert picked is not None
    name, argv = picked
    assert name == "dnf"
    assert argv[0] == "dnf"
    assert "-y" in argv  # never interactive


def test_package_manager_prefers_apt_get_over_others_on_linux(monkeypatch) -> None:
    monkeypatch.setattr(node_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: f"/usr/bin/{name}")
    name, _argv = node_check._package_manager_command()
    assert name == "apt-get"


def test_package_manager_none_available_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(node_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: None)
    assert node_check._package_manager_command() is None


def test_package_manager_macos_uses_brew(monkeypatch) -> None:
    monkeypatch.setattr(node_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        node_check.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
    )
    name, argv = node_check._package_manager_command()
    assert name == "brew"
    assert argv == ["brew", "install", "node"]


def test_package_manager_windows_prefers_winget(monkeypatch) -> None:
    monkeypatch.setattr(node_check.platform, "system", lambda: "Windows")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: f"C:\\{name}.exe")
    name, argv = node_check._package_manager_command()
    assert name == "winget"
    assert "--accept-package-agreements" in argv


def test_package_manager_unknown_os_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(node_check.platform, "system", lambda: "SomeOtherOS")
    assert node_check._package_manager_command() is None


# ---------------------------------------------------------------------------
# ensure_node_installed()
# ---------------------------------------------------------------------------


def test_ensure_node_installed_skips_install_when_already_available(
    monkeypatch,
) -> None:
    install_calls = []

    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            return _FakeCompletedProcess(0, stdout="v20.0.0")
        install_calls.append(argv)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    result = node_check.ensure_node_installed(print_progress=False)
    assert result["available"] is True
    assert install_calls == []  # never even looked at a package manager


def test_ensure_node_installed_no_package_manager_available(monkeypatch) -> None:
    monkeypatch.setattr(
        node_check.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(node_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: None)

    result = node_check.ensure_node_installed(print_progress=False)
    assert result["available"] is False
    assert "no supported package manager" in result["error"]


def test_ensure_node_installed_succeeds_end_to_end(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileNotFoundError()  # not installed yet
            return _FakeCompletedProcess(0, stdout="v20.11.0")  # now it is
        if argv[0] == "apt-get":
            return _FakeCompletedProcess(0)  # install succeeded
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        node_check.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    result = node_check.ensure_node_installed(print_progress=False)
    assert result["available"] is True
    assert result["version"] == "v20.11.0"


def test_ensure_node_installed_reports_install_command_failure(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            raise FileNotFoundError()
        if argv[0] == "brew":
            return _FakeCompletedProcess(1, stderr="Error: some brew failure\n")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: "/usr/local/bin/brew")

    result = node_check.ensure_node_installed(print_progress=False)
    assert result["available"] is False
    assert "brew exited with code 1" in result["error"]
    assert "some brew failure" in result["error"]


def test_ensure_node_installed_suggests_sudo_on_linux_when_not_root(
    monkeypatch,
) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            raise FileNotFoundError()
        if argv[0] == "apt-get":
            return _FakeCompletedProcess(1, stderr="Permission denied\n")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        node_check.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )
    monkeypatch.setattr(node_check.os, "geteuid", lambda: 1000, raising=False)

    result = node_check.ensure_node_installed(print_progress=False)
    assert result["available"] is False
    assert "sudo apt-get install -y nodejs" in result["error"]


def test_ensure_node_installed_never_prepends_sudo_itself(monkeypatch) -> None:
    """The install command actually executed must never contain sudo/runas —
    see the module docstring for why privilege escalation is never silent.
    """
    executed = []

    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            raise FileNotFoundError()
        executed.append(argv)
        return _FakeCompletedProcess(1, stderr="denied")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        node_check.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    node_check.ensure_node_installed(print_progress=False)
    assert len(executed) == 1
    assert "sudo" not in executed[0]
    assert "runas" not in executed[0]


def test_ensure_node_installed_times_out(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            raise FileNotFoundError()
        raise subprocess.TimeoutExpired(cmd=argv, timeout=300)

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: "/usr/local/bin/brew")

    result = node_check.ensure_node_installed(print_progress=False)
    assert result["available"] is False
    assert "Timed out" in result["error"]


def test_ensure_node_installed_detects_stale_path_after_apparent_success(
    monkeypatch,
) -> None:
    """Install command reports success, but `node` is still nowhere to be
    found afterwards (e.g. needs a fresh shell for PATH to pick it up) —
    must be reported as its own distinct condition, not a false "available".
    """

    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            raise FileNotFoundError()
        if argv[0] == "brew":
            return _FakeCompletedProcess(0)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: "/usr/local/bin/brew")

    result = node_check.ensure_node_installed(print_progress=False)
    assert result["available"] is False
    assert "still isn't on PATH" in result["error"]


def test_ensure_node_installed_prints_progress_by_default(monkeypatch, capsys) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            raise FileNotFoundError()
        return _FakeCompletedProcess(1, stderr="nope")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: "/usr/local/bin/brew")

    node_check.ensure_node_installed()  # print_progress defaults to True
    out = capsys.readouterr().out
    assert "attempting to install it automatically" in out
    assert "brew" in out


def test_ensure_node_installed_silent_when_asked(monkeypatch, capsys) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["node", "-v"]:
            raise FileNotFoundError()
        return _FakeCompletedProcess(1, stderr="nope")

    monkeypatch.setattr(node_check.subprocess, "run", fake_run)
    monkeypatch.setattr(node_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node_check.shutil, "which", lambda name: "/usr/local/bin/brew")

    node_check.ensure_node_installed(print_progress=False)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# print_node_warning()
# ---------------------------------------------------------------------------


def test_print_node_warning_silent_when_available(capsys) -> None:
    result = node_check.print_node_warning(
        {"available": True, "version": "v20", "error": ""}
    )
    out = capsys.readouterr().out
    assert out == ""
    assert result["available"] is True


def test_print_node_warning_prints_guidance_when_missing(capsys) -> None:
    node_check.print_node_warning(
        {"available": False, "version": "", "error": "node not found in PATH"}
    )
    out = capsys.readouterr().out
    assert "Node.js NOT FOUND" in out
    assert "node not found in PATH" in out
    assert "nodejs.org" in out

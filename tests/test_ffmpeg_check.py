"""Tests for core/ffmpeg_check.py's ensure_ffmpeg_installed() — the
best-effort ffmpeg auto-install added to mirror core/node_check.py's
ensure_node_installed(). check_ffmpeg()/print_ffmpeg_warning() themselves
were already covered by test_core_logic.py before this file existed; this
file focuses on the auto-install machinery only. Every subprocess call is
mocked: this suite must never actually invoke a real package manager or
modify the machine it runs on.
"""

from __future__ import annotations

import subprocess

from SpotiFLAC.core import ffmpeg_check


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# _package_manager_command()
# ---------------------------------------------------------------------------


def test_package_manager_picks_first_available_on_linux(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ffmpeg_check.shutil,
        "which",
        lambda name: "/usr/bin/dnf" if name == "dnf" else None,
    )
    picked = ffmpeg_check._package_manager_command()
    assert picked is not None
    name, argv = picked
    assert name == "dnf"
    assert argv[0] == "dnf"
    assert "-y" in argv  # never interactive


def test_package_manager_prefers_apt_get_over_others_on_linux(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ffmpeg_check.shutil, "which", lambda name: f"/usr/bin/{name}")
    name, _argv = ffmpeg_check._package_manager_command()
    assert name == "apt-get"


def test_package_manager_none_available_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ffmpeg_check.shutil, "which", lambda name: None)
    assert ffmpeg_check._package_manager_command() is None


def test_package_manager_macos_uses_brew(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        ffmpeg_check.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
    )
    name, argv = ffmpeg_check._package_manager_command()
    assert name == "brew"
    assert argv == ["brew", "install", "ffmpeg"]


def test_package_manager_windows_prefers_winget(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Windows")
    monkeypatch.setattr(ffmpeg_check.shutil, "which", lambda name: f"C:\\{name}.exe")
    name, argv = ffmpeg_check._package_manager_command()
    assert name == "winget"
    assert "--accept-package-agreements" in argv
    assert "Gyan.FFmpeg" in argv


def test_package_manager_unknown_os_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "SomeOtherOS")
    assert ffmpeg_check._package_manager_command() is None


# ---------------------------------------------------------------------------
# ensure_ffmpeg_installed()
# ---------------------------------------------------------------------------


def test_ensure_ffmpeg_installed_skips_install_when_already_available(
    monkeypatch,
) -> None:
    install_calls = []

    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            return _FakeCompletedProcess(0, stdout="ffmpeg version 7.0\n")
        install_calls.append(argv)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    result = ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert result["available"] is True
    assert install_calls == []  # never even looked at a package manager


def test_ensure_ffmpeg_installed_no_package_manager_available(monkeypatch) -> None:
    monkeypatch.setattr(
        ffmpeg_check.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ffmpeg_check.shutil, "which", lambda name: None)

    result = ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert result["available"] is False
    assert "no supported package manager" in result["error"]


def test_ensure_ffmpeg_installed_succeeds_end_to_end(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileNotFoundError()  # not installed yet
            return _FakeCompletedProcess(0, stdout="ffmpeg version 7.0\n")  # now it is
        if argv[0] == "apt-get":
            return _FakeCompletedProcess(0)  # install succeeded
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ffmpeg_check.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    result = ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert result["available"] is True
    assert "ffmpeg version 7.0" in result["version"]


def test_ensure_ffmpeg_installed_reports_install_command_failure(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            raise FileNotFoundError()
        if argv[0] == "brew":
            return _FakeCompletedProcess(1, stderr="Error: some brew failure\n")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        ffmpeg_check.shutil, "which", lambda name: "/usr/local/bin/brew"
    )

    result = ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert result["available"] is False
    assert "brew exited with code 1" in result["error"]
    assert "some brew failure" in result["error"]


def test_ensure_ffmpeg_installed_suggests_sudo_on_linux_when_not_root(
    monkeypatch,
) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            raise FileNotFoundError()
        if argv[0] == "apt-get":
            return _FakeCompletedProcess(1, stderr="Permission denied\n")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ffmpeg_check.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )
    monkeypatch.setattr(ffmpeg_check.os, "geteuid", lambda: 1000, raising=False)

    result = ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert result["available"] is False
    assert "sudo apt-get install -y ffmpeg" in result["error"]


def test_ensure_ffmpeg_installed_never_prepends_sudo_itself(monkeypatch) -> None:
    """The install command actually executed must never contain sudo/runas —
    see the module docstring for why privilege escalation is never silent.
    """
    executed = []

    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            raise FileNotFoundError()
        executed.append(argv)
        return _FakeCompletedProcess(1, stderr="denied")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        ffmpeg_check.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert len(executed) == 1
    assert "sudo" not in executed[0]
    assert "runas" not in executed[0]


def test_ensure_ffmpeg_installed_times_out(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            raise FileNotFoundError()
        raise subprocess.TimeoutExpired(cmd=argv, timeout=300)

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        ffmpeg_check.shutil, "which", lambda name: "/usr/local/bin/brew"
    )

    result = ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert result["available"] is False
    assert "Timed out" in result["error"]


def test_ensure_ffmpeg_installed_detects_stale_path_after_apparent_success(
    monkeypatch,
) -> None:
    """Install command reports success, but `ffmpeg` is still nowhere to be
    found afterwards (e.g. needs a fresh shell for PATH to pick it up) —
    must be reported as its own distinct condition, not a false "available".
    """

    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            raise FileNotFoundError()
        if argv[0] == "brew":
            return _FakeCompletedProcess(0)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        ffmpeg_check.shutil, "which", lambda name: "/usr/local/bin/brew"
    )

    result = ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert result["available"] is False
    assert "still isn't on PATH" in result["error"]


def test_ensure_ffmpeg_installed_prints_progress_by_default(
    monkeypatch, capsys
) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            raise FileNotFoundError()
        return _FakeCompletedProcess(1, stderr="nope")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        ffmpeg_check.shutil, "which", lambda name: "/usr/local/bin/brew"
    )

    ffmpeg_check.ensure_ffmpeg_installed()  # print_progress defaults to True
    out = capsys.readouterr().out
    assert "attempting to install it automatically" in out
    assert "brew" in out


def test_ensure_ffmpeg_installed_silent_when_asked(monkeypatch, capsys) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["ffmpeg", "-version"]:
            raise FileNotFoundError()
        return _FakeCompletedProcess(1, stderr="nope")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_check.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        ffmpeg_check.shutil, "which", lambda name: "/usr/local/bin/brew"
    )

    ffmpeg_check.ensure_ffmpeg_installed(print_progress=False)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# print_ffmpeg_warning() — the auto-install mention added to its text
# ---------------------------------------------------------------------------


def test_print_ffmpeg_warning_mentions_transcode_auto_install(capsys) -> None:
    ffmpeg_check.print_ffmpeg_warning(
        {"available": False, "version": "", "error": "ffmpeg not found in PATH"}
    )
    out = capsys.readouterr().out
    assert "ffmpeg NOT FOUND" in out
    assert "automatically" in out
    assert "ffmpeg.org" in out

"""Regression tests for temporary Chromium profile cleanup."""

from pathlib import Path

from SpotiFLAC.core import solver


def test_build_chromium_options_keeps_the_created_temp_profile(monkeypatch):
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)
    monkeypatch.setattr(solver, "_find_chrome", lambda: "/usr/bin/chromium")

    options, profile_dir = solver.build_chromium_options()

    try:
        assert Path(profile_dir).is_dir()
        user_data_args = [
            arg for arg in options.arguments if arg.startswith("--user-data-dir=")
        ]
        assert user_data_args == [f"--user-data-dir={profile_dir}"]
    finally:
        solver._cleanup_profile_dir(profile_dir)


def test_cleanup_removes_solver_owned_profile_and_cache(monkeypatch):
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)
    profile_dir = Path(solver._get_profile_dir())
    cache_file = profile_dir / "Default" / "Cache" / "data_0"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"temporary chromium cache")

    solver._cleanup_profile_dir(str(profile_dir))

    assert not profile_dir.exists()


def test_cleanup_preserves_configured_profile(monkeypatch, tmp_path):
    profile_dir = tmp_path / "ts_profile_user_managed"
    cache_file = profile_dir / "Default" / "Cache" / "data_0"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"keep user-managed profile")
    monkeypatch.setenv("TS_PROFILE_DIR", str(profile_dir))

    solver._cleanup_profile_dir(str(profile_dir))

    assert cache_file.read_bytes() == b"keep user-managed profile"


def test_cleanup_refuses_to_remove_unmanaged_chrome_directory(monkeypatch, tmp_path):
    profile_dir = tmp_path / "com.google.Chrome.example"
    profile_dir.mkdir()
    marker = profile_dir / "keep.txt"
    marker.write_text("not owned by SpotiFLAC", encoding="utf-8")
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)

    solver._cleanup_profile_dir(str(profile_dir))

    assert marker.exists()


def test_pydoll_temp_directory_manager_is_explicitly_cleaned():
    class FakeManager:
        cleaned = False

        def cleanup(self):
            self.cleaned = True

    class FakeBrowser:
        _temp_directory_manager = FakeManager()

    browser = FakeBrowser()
    solver._cleanup_pydoll_temp_dirs(browser)

    assert browser._temp_directory_manager.cleaned


def test_browser_temp_dir_lives_inside_the_owned_profile(monkeypatch):
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)
    profile_dir = solver._get_profile_dir()

    try:
        tmp_dir = Path(solver._browser_tmp_dir(profile_dir))
        assert tmp_dir.parent == Path(profile_dir)
    finally:
        solver._cleanup_profile_dir(profile_dir)


def test_browser_is_launched_with_its_own_temp_dir(monkeypatch):
    """Chromium's own /tmp/com.google.Chrome.* dirs must land in the profile."""
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)
    profile_dir = solver._get_profile_dir()
    seen = {}

    class FakeProcess:
        pid = 1

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return FakeProcess()

    class FakeBrowser:
        _browser_process_manager = None

    monkeypatch.setattr(solver.subprocess, "Popen", fake_popen)
    browser = FakeBrowser()

    try:
        solver.isolate_browser_temp_dir(browser, profile_dir)
        browser._browser_process_manager.start_browser_process(
            "/usr/bin/chromium", 9222, []
        )

        tmp_dir = solver._browser_tmp_dir(profile_dir)
        assert Path(tmp_dir).is_dir()
        assert seen["env"]["TMPDIR"] == tmp_dir
        assert seen["env"]["TMP"] == tmp_dir
        assert seen["env"]["TEMP"] == tmp_dir
    finally:
        solver._cleanup_profile_dir(profile_dir)

    assert not Path(profile_dir).exists()


def test_configured_profile_keeps_its_data_but_loses_the_browser_temp_dir(
    monkeypatch, tmp_path
):
    profile_dir = tmp_path / "persistent-profile"
    (profile_dir / "Default").mkdir(parents=True)
    monkeypatch.setenv("TS_PROFILE_DIR", str(profile_dir))
    monkeypatch.setattr(solver, "_profile_base_dir", lambda: str(tmp_path))

    class FakeBrowser:
        _browser_process_manager = None

    solver.isolate_browser_temp_dir(FakeBrowser(), str(profile_dir))
    tmp_dir = Path(solver._browser_tmp_dir(str(profile_dir)))
    assert tmp_dir.is_dir()
    assert profile_dir not in tmp_dir.parents
    (tmp_dir / "com.google.Chrome.abc123").mkdir()

    solver._cleanup_profile_dir(str(profile_dir))

    assert not tmp_dir.exists()
    assert (profile_dir / "Default").is_dir()


def test_describing_a_failed_start_does_not_create_a_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)
    monkeypatch.setattr(solver, "_profile_base_dir", lambda: str(tmp_path))

    message = solver._describe_browser_start_error(
        RuntimeError("boom"), None, "/tmp/ts_profile_used"
    )

    assert "profile_dir=/tmp/ts_profile_used" in message
    assert list(tmp_path.iterdir()) == []


def _aged(path: Path, seconds: float) -> Path:
    import os
    import time

    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def test_stale_sweep_removes_only_old_unused_leftovers(monkeypatch, tmp_path):
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)
    monkeypatch.setattr(solver, "_profile_base_dir", lambda: str(tmp_path))
    in_use = tmp_path / "ts_profile_running"
    monkeypatch.setattr(solver, "_profile_in_use", lambda path: path == str(in_use))
    day = 24 * 60 * 60

    stale_profile = tmp_path / "ts_profile_stale"
    (stale_profile / "Default" / "Cache").mkdir(parents=True)
    (stale_profile / "Default" / "Cache" / "data_0").write_bytes(b"x" * 64)
    stale_tmp = tmp_path / "ts_tmp_0123456789ab"
    fresh = tmp_path / "ts_profile_fresh"
    foreign = tmp_path / "com.google.Chrome.0CsoCE"
    lookalike_file = tmp_path / "ts_profile_file"
    for directory in (stale_tmp, fresh, foreign, in_use):
        directory.mkdir()
    lookalike_file.write_text("not a directory", encoding="utf-8")
    for path in (stale_profile, stale_tmp, foreign, in_use, lookalike_file):
        _aged(path, 2 * day)

    assert solver._sweep_stale_browser_dirs() == 2

    assert not stale_profile.exists()
    assert not stale_tmp.exists()
    assert fresh.is_dir()
    assert foreign.is_dir()
    assert in_use.is_dir()
    assert lookalike_file.is_file()


def test_stale_sweep_spares_profiles_this_process_still_uses(monkeypatch, tmp_path):
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)
    monkeypatch.setattr(solver, "_profile_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(solver, "_profile_in_use", lambda path: False)
    profile_dir = Path(solver._get_profile_dir())
    _aged(profile_dir, 3 * 24 * 60 * 60)

    try:
        assert solver._sweep_stale_browser_dirs() == 0
        assert profile_dir.is_dir()
    finally:
        solver._cleanup_profile_dir(str(profile_dir))

    assert not profile_dir.exists()


def test_stale_sweep_waits_longer_when_liveness_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(solver, "_profile_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(solver, "_profile_in_use", lambda path: None)
    two_hours = tmp_path / "ts_profile_two_hours"
    two_days = tmp_path / "ts_profile_two_days"
    two_hours.mkdir()
    two_days.mkdir()
    _aged(two_hours, 2 * 60 * 60)
    _aged(two_days, 2 * 24 * 60 * 60)

    assert solver._sweep_stale_browser_dirs() == 1

    assert two_hours.is_dir()
    assert not two_days.exists()

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

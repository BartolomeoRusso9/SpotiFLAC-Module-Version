from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from SpotiFLAC.core import solver


def test_browser_start_tracks_scoped_chrome_temp_dirs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(solver, "_chrome_temp_roots", lambda: (str(tmp_path),))
    temp_dir = tmp_path / ".com.google.Chrome.created-by-solver"

    class FakeBrowser:
        async def start(self):
            temp_dir.mkdir()
            (temp_dir / "cache.bin").write_bytes(b"temporary browser data")
            return "tab"

    browser = FakeBrowser()
    result = asyncio.run(solver._start_browser_with_temp_tracking(browser))

    assert result == "tab"
    assert tuple(browser._spotiflac_chrome_temp_dirs) == (str(temp_dir),)


def test_browser_start_tracks_temp_dirs_even_when_start_fails(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(solver, "_chrome_temp_roots", lambda: (str(tmp_path),))
    temp_dir = tmp_path / ".com.google.Chrome.failed-start"

    class FakeBrowser:
        async def start(self):
            temp_dir.mkdir()
            raise RuntimeError("startup failed")

    browser = FakeBrowser()
    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(solver._start_browser_with_temp_tracking(browser))

    assert tuple(browser._spotiflac_chrome_temp_dirs) == (str(temp_dir),)


def test_cleanup_removes_owned_temp_dir_and_ephemeral_profile(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(solver, "_chrome_temp_roots", lambda: (str(tmp_path),))
    monkeypatch.setattr(solver, "_active_chrome_temp_dir_names", lambda: set())
    monkeypatch.delenv("TS_PROFILE_DIR", raising=False)

    temp_dir = tmp_path / ".com.google.Chrome.owned"
    temp_dir.mkdir()
    (temp_dir / "cache.bin").write_bytes(b"temporary data")
    profile_dir = tmp_path / "ts_profile_owned"
    profile_dir.mkdir()
    (profile_dir / "Cache").mkdir()

    browser = SimpleNamespace(_spotiflac_chrome_temp_dirs=(str(temp_dir),))
    solver._cleanup_browser_temp_files(browser, str(profile_dir))

    assert not temp_dir.exists()
    assert not profile_dir.exists()
    assert browser._spotiflac_chrome_temp_dirs == ()


def test_cleanup_preserves_custom_profile_and_temp_dirs_used_by_live_browser(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(solver, "_chrome_temp_roots", lambda: (str(tmp_path),))
    monkeypatch.setenv("TS_PROFILE_DIR", str(tmp_path / "user-profile"))

    temp_dir = tmp_path / ".com.google.Chrome.still-running"
    temp_dir.mkdir()
    profile_dir = tmp_path / "user-profile"
    profile_dir.mkdir()
    (profile_dir / "important-state").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(
        solver, "_active_chrome_temp_dir_names", lambda: {temp_dir.name}
    )
    browser = SimpleNamespace(_spotiflac_chrome_temp_dirs=(str(temp_dir),))
    solver._cleanup_browser_temp_files(browser, str(profile_dir))

    assert temp_dir.is_dir()
    assert (profile_dir / "important-state").read_text(encoding="utf-8") == "keep"


def test_stale_inactive_chrome_temp_dirs_are_removed_but_active_ones_are_kept(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(solver, "_chrome_temp_roots", lambda: (str(tmp_path),))
    monkeypatch.setattr(solver, "_CHROME_TEMP_DIR_MAX_AGE_SECONDS", 60)
    stale = tmp_path / ".com.google.Chrome.stale"
    active = tmp_path / ".com.google.Chrome.active"
    stale.mkdir()
    active.mkdir()
    old = time.time() - 600
    os.utime(stale, (old, old))
    os.utime(active, (old, old))
    monkeypatch.setattr(
        solver, "_active_chrome_temp_dir_names", lambda: {active.name}
    )

    solver._cleanup_stale_chrome_temp_dirs()

    assert not stale.exists()
    assert active.is_dir()


def test_build_options_does_not_wipe_explicit_profile(tmp_path, monkeypatch) -> None:
    profile_dir = tmp_path / "persistent-profile"
    profile_dir.mkdir()
    marker = profile_dir / "important-state"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("TS_PROFILE_DIR", str(profile_dir))
    monkeypatch.setattr(solver, "_find_chrome", lambda: "/usr/bin/chromium")

    _options, returned_profile = solver.build_chromium_options()

    assert returned_profile == str(profile_dir)
    assert marker.read_text(encoding="utf-8") == "keep"

"""$MORNYE_DATA_DIR / $MORNYE_DOWNLOAD_DIR relocate what $HOME decided."""

from pathlib import Path

from mornye.core import paths


def test_defaults_stay_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MORNYE_DATA_DIR", raising=False)
    monkeypatch.delenv("MORNYE_DOWNLOAD_DIR", raising=False)
    monkeypatch.delenv("MORNYE_CACHE_DIR", raising=False)

    assert paths.data_dir() == Path.home() / ".mornye"
    assert paths.cache_dir() == Path.home() / ".mornye" / ".cache"
    assert paths.default_download_dir() == str(Path.home() / "Music" / "Mornye")


def test_data_dir_override_moves_data_and_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNYE_DATA_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("MORNYE_CACHE_DIR", raising=False)

    assert paths.data_dir() == tmp_path / "config"
    assert paths.data_path("mornye.db") == tmp_path / "config" / "mornye.db"
    assert paths.cache_dir() == tmp_path / "config" / ".cache"


def test_cache_dir_override_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNYE_DATA_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MORNYE_CACHE_DIR", str(tmp_path / "cache"))

    assert paths.cache_dir() == tmp_path / "cache"


def test_download_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNYE_DOWNLOAD_DIR", str(tmp_path / "music"))

    assert paths.default_download_dir() == str(tmp_path / "music")


def test_blank_overrides_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Compose files pass "${VAR:-}", i.e. an empty string, for anything unset.
    monkeypatch.setenv("MORNYE_DATA_DIR", "  ")
    monkeypatch.setenv("MORNYE_DOWNLOAD_DIR", "")

    assert paths.data_dir() == Path.home() / ".mornye"
    assert paths.default_download_dir() == str(Path.home() / "Music" / "Mornye")

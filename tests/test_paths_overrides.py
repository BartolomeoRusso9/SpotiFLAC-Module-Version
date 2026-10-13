"""$SPOTIFLAC_DATA_DIR / $SPOTIFLAC_DOWNLOAD_DIR relocate what $HOME decided."""

from pathlib import Path

from SpotiFLAC.core import paths


def test_defaults_stay_under_home(monkeypatch):
    monkeypatch.delenv("SPOTIFLAC_DATA_DIR", raising=False)
    monkeypatch.delenv("SPOTIFLAC_DOWNLOAD_DIR", raising=False)
    monkeypatch.delenv("SPOTIFLAC_CACHE_DIR", raising=False)

    assert paths.data_dir() == Path.home() / ".spotiflac"
    assert paths.cache_dir() == Path.home() / ".spotiflac" / ".cache"
    assert paths.default_download_dir() == str(Path.home() / "Music" / "SpotiFLAC")


def test_data_dir_override_moves_data_and_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFLAC_DATA_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("SPOTIFLAC_CACHE_DIR", raising=False)

    assert paths.data_dir() == tmp_path / "config"
    assert paths.data_path("spotiflac.db") == tmp_path / "config" / "spotiflac.db"
    assert paths.cache_dir() == tmp_path / "config" / ".cache"


def test_cache_dir_override_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFLAC_DATA_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "cache"))

    assert paths.cache_dir() == tmp_path / "cache"


def test_download_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFLAC_DOWNLOAD_DIR", str(tmp_path / "music"))

    assert paths.default_download_dir() == str(tmp_path / "music")


def test_blank_overrides_are_ignored(monkeypatch):
    # Compose files pass "${VAR:-}", i.e. an empty string, for anything unset.
    monkeypatch.setenv("SPOTIFLAC_DATA_DIR", "  ")
    monkeypatch.setenv("SPOTIFLAC_DOWNLOAD_DIR", "")

    assert paths.data_dir() == Path.home() / ".spotiflac"
    assert paths.default_download_dir() == str(Path.home() / "Music" / "SpotiFLAC")

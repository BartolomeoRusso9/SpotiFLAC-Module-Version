from __future__ import annotations

import logging
import warnings
from pathlib import Path

import pytest

from mornye.core import db as db_module
from mornye.core import env as env_module
from mornye.core import paths


@pytest.fixture(autouse=True)
def _reset_legacy_warnings(monkeypatch):
    monkeypatch.setattr(env_module, "_warned_legacy_names", set())
    monkeypatch.setattr(paths, "_warned_legacy_locations", set())


def test_legacy_environment_value_is_used_with_deprecation_warning(monkeypatch, caplog):
    monkeypatch.delenv("MORNYE_REGISTRIES", raising=False)
    monkeypatch.setenv("SPOTIFLAC_REGISTRIES", "https://registry.invalid/list.json")

    with caplog.at_level(logging.WARNING, logger="mornye.config"):
        value = env_module.env_value("MORNYE_REGISTRIES")

    assert value == "https://registry.invalid/list.json"
    assert "SPOTIFLAC_REGISTRIES" in caplog.text
    assert "registry.invalid" not in caplog.text


def test_canonical_environment_value_wins_conflict(monkeypatch, caplog):
    monkeypatch.setenv("MORNYE_DATA_DIR", "/new/state")
    monkeypatch.setenv("SPOTIFLAC_DATA_DIR", "/old/state")

    with caplog.at_level(logging.WARNING, logger="mornye.config"):
        value = env_module.env_value("MORNYE_DATA_DIR")

    assert value == "/new/state"
    assert "Ignoring deprecated SPOTIFLAC_DATA_DIR" in caplog.text
    assert "/new/state" not in caplog.text
    assert "/old/state" not in caplog.text


def test_existing_legacy_data_directory_remains_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MORNYE_DATA_DIR", raising=False)
    monkeypatch.delenv("SPOTIFLAC_DATA_DIR", raising=False)
    legacy = tmp_path / ".spotiflac"
    legacy.mkdir()
    sentinel = legacy / "user-data"
    sentinel.write_text("preserved", encoding="utf-8")

    assert paths.data_dir() == legacy
    assert sentinel.read_text(encoding="utf-8") == "preserved"
    assert not (tmp_path / ".mornye").exists()


def test_new_data_directory_wins_without_modifying_legacy_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MORNYE_DATA_DIR", raising=False)
    monkeypatch.delenv("SPOTIFLAC_DATA_DIR", raising=False)
    current = tmp_path / ".mornye"
    legacy = tmp_path / ".spotiflac"
    current.mkdir()
    legacy.mkdir()
    current_file = current / "current"
    legacy_file = legacy / "legacy"
    current_file.write_text("current", encoding="utf-8")
    legacy_file.write_text("legacy", encoding="utf-8")

    assert paths.data_dir() == current
    assert current_file.read_text(encoding="utf-8") == "current"
    assert legacy_file.read_text(encoding="utf-8") == "legacy"


def test_existing_legacy_download_directory_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MORNYE_DOWNLOAD_DIR", raising=False)
    monkeypatch.delenv("SPOTIFLAC_DOWNLOAD_DIR", raising=False)
    legacy = tmp_path / "Music" / "SpotiFLAC"
    legacy.mkdir(parents=True)

    assert Path(paths.default_download_dir()) == legacy


def test_legacy_cache_settings_are_copied_without_removing_the_source(
    tmp_path, monkeypatch
):
    legacy_cache = tmp_path / "old-cache"
    legacy_cache.mkdir()
    source = legacy_cache / "profiles.json"
    source.write_text('{"profiles": []}', encoding="utf-8")
    monkeypatch.setattr(paths, "LEGACY_CACHE_DIR", legacy_cache)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MORNYE_CACHE_DIR", raising=False)
    monkeypatch.delenv("SPOTIFLAC_CACHE_DIR", raising=False)
    target = tmp_path / ".mornye" / ".cache" / "profiles.json"

    paths.adopt_legacy_cache_file(target)

    assert target.read_text(encoding="utf-8") == '{"profiles": []}'
    assert source.read_text(encoding="utf-8") == '{"profiles": []}'


def test_database_uses_legacy_filename_until_new_file_exists(tmp_path, monkeypatch):
    current = tmp_path / ".mornye" / "mornye.db"
    legacy = tmp_path / ".mornye" / "spotiflac.db"
    legacy.parent.mkdir()
    legacy.touch()
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", current)
    monkeypatch.setattr(db_module, "LEGACY_DB_PATH", legacy)
    monkeypatch.delenv("MORNYE_DB_PATH", raising=False)
    monkeypatch.delenv("SPOTIFLAC_DB_PATH", raising=False)

    assert db_module.db_path() == legacy

    current.touch()
    assert db_module.db_path() == current
    assert legacy.exists()


def test_explicit_canonical_database_path_wins_environment_conflict(
    tmp_path, monkeypatch
):
    current = tmp_path / "new.db"
    legacy = tmp_path / "old.db"
    monkeypatch.setenv("MORNYE_DB_PATH", str(current))
    monkeypatch.setenv("SPOTIFLAC_DB_PATH", str(legacy))

    assert db_module.db_path() == current


def test_legacy_public_names_are_deprecated_aliases():
    import mornye

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        old_sync = mornye.SpotiFLAC
        old_async = mornye.AsyncSpotiFLAC
        old_config = mornye.SpotiFLACConfig

    assert old_sync is mornye.Mornye
    assert old_async is mornye.AsyncMornye
    assert old_config is mornye.MornyeConfig
    assert len(caught) == 3
    assert all(item.category is DeprecationWarning for item in caught)


def test_registry_environment_file_prefers_canonical_key_on_conflict(
    tmp_path, monkeypatch, caplog
):
    from mornye.extensions import registry_config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MORNYE_REGISTRIES=https://new.invalid/registry.json\n"
        "SPOTIFLAC_REGISTRIES=https://old.invalid/registry.json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_config, "ENV_FILES_TO_CHECK", (env_file,))

    with caplog.at_level(logging.WARNING, logger=registry_config.__name__):
        values = registry_config._env_file_urls()

    assert set(values) == {"https://new.invalid/registry.json"}
    assert "conflicting legacy registry configuration" in caplog.text
    assert "old.invalid" not in caplog.text


def test_legacy_minimum_trust_variable_is_used(monkeypatch):
    from mornye.extensions.trust import resolve_min_trust

    monkeypatch.delenv("MORNYE_MIN_TRUST", raising=False)
    monkeypatch.setenv("SPOTIFLAC_MIN_TRUST", "signed")

    assert resolve_min_trust() == "signed"


def test_legacy_lyrics_flag_is_used(monkeypatch):
    from mornye.core.lyrics import _env_flag

    monkeypatch.delenv("MORNYE_LYRICS_JELLYFIN_COMPAT", raising=False)
    monkeypatch.setenv("SPOTIFLAC_LYRICS_JELLYFIN_COMPAT", "true")

    assert _env_flag("MORNYE_LYRICS_JELLYFIN_COMPAT")


def test_legacy_extension_sandbox_options_are_forwarded(monkeypatch):
    from mornye.extensions.sandbox import build_env

    for name in (
        "MORNYE_EXT_ALLOW_PRIVATE_NETWORK",
        "MORNYE_EXT_ALLOW_ANY_WRITE",
        "MORNYE_EXT_WRITABLE_DIRS",
        "MORNYE_EXT_NO_SANDBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setenv("SPOTIFLAC_EXT_WRITABLE_DIRS", "/tmp/mornye-extension-test")

    extension_env = build_env()

    assert extension_env["MORNYE_EXT_ALLOW_PRIVATE_NETWORK"] == "1"
    assert extension_env["MORNYE_EXT_WRITABLE_DIRS"] == "/tmp/mornye-extension-test"


def test_legacy_extension_directory_variable_is_used(tmp_path, monkeypatch):
    from mornye.extensions.manager import ExtensionManager

    configured = tmp_path / "extensions"
    monkeypatch.delenv("MORNYE_EXT_DIR", raising=False)
    monkeypatch.setenv("SPOTIFLAC_EXT_DIR", str(configured))

    manager = ExtensionManager(auto_install_downloads=False)

    assert manager.ext_dir == configured
    assert configured.is_dir()


def test_legacy_web_token_environment_variable_is_used(monkeypatch):
    from mornye.webapp import resolve_web_token

    monkeypatch.delenv("MORNYE_WEB_TOKEN", raising=False)
    monkeypatch.setenv("SPOTIFLAC_WEB_TOKEN", "migration-test-token")

    assert resolve_web_token(None) == "migration-test-token"

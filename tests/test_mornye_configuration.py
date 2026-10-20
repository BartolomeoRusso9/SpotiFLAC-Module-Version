from __future__ import annotations

from pathlib import Path

from mornye.core import db as db_module
from mornye.core import env as env_module
from mornye.core import paths


def test_environment_values_use_the_configured_name(monkeypatch):
    monkeypatch.setenv("MORNYE_REGISTRIES", "https://registry.invalid/list.json")
    assert env_module.env_value("MORNYE_REGISTRIES") == (
        "https://registry.invalid/list.json"
    )
    assert env_module.env_value("MORNYE_UNSET") is None


def test_data_directory_uses_the_current_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.delenv("MORNYE_DATA_DIR", raising=False)

    assert paths.data_dir() == tmp_path / ".mornye"


def test_download_directory_uses_the_current_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.delenv("MORNYE_DOWNLOAD_DIR", raising=False)

    assert Path(paths.default_download_dir()) == tmp_path / "Music" / "Mornye"


def test_database_path_uses_current_filename_and_environment_override(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", tmp_path / "state" / "mornye.db")
    monkeypatch.delenv("MORNYE_DB_PATH", raising=False)

    assert db_module.db_path() == tmp_path / "state" / "mornye.db"

    configured = tmp_path / "custom.db"
    monkeypatch.setenv("MORNYE_DB_PATH", str(configured))
    assert db_module.db_path() == configured


def test_registry_environment_file_reads_canonical_key(tmp_path, monkeypatch):
    from mornye.extensions import registry_config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MORNYE_REGISTRIES=https://registry.invalid/list.json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_config, "ENV_FILES_TO_CHECK", (env_file,))

    assert registry_config._env_file_urls() == {
        "https://registry.invalid/list.json": [env_file]
    }


def test_public_exports_use_current_api_names():
    import mornye

    assert "Mornye" in mornye.__all__
    assert "AsyncMornye" in mornye.__all__
    assert "MornyeConfig" in mornye.__all__

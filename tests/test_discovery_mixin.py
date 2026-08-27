"""Tests for api_mixins/discovery.py (the "Discover registries" GUI/web
surface) — thin-wrapper behavior only; extensions/directories.py's own
tests cover the actual logic.
"""

from __future__ import annotations

from SpotiFLAC.api_mixins.discovery import DiscoveryMixin
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.webapp import ALLOWED_METHODS


def test_discovery_methods_are_web_allowed_and_resolve_to_the_mixin() -> None:
    for name in (
        "get_registry_directories",
        "add_registry_directory",
        "remove_registry_directory",
        "discover_registries",
    ):
        assert name in ALLOWED_METHODS
        assert name in DiscoveryMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith("DiscoveryMixin.")


def test_get_registry_directories_empty_by_default(tmp_path, monkeypatch):
    from SpotiFLAC.extensions import directories

    monkeypatch.setattr(
        directories, "CONFIG_FILE", tmp_path / "directory_settings.json"
    )
    monkeypatch.delenv(directories.DIRECTORY_ENV_KEY, raising=False)
    monkeypatch.setattr(directories, "ENV_FILES_TO_CHECK", ())

    api = SpotiFLAC_API()
    assert api.get_registry_directories() == []


def test_add_and_remove_registry_directory_round_trip(tmp_path, monkeypatch):
    from SpotiFLAC.extensions import directories

    monkeypatch.setattr(
        directories, "CONFIG_FILE", tmp_path / "directory_settings.json"
    )
    monkeypatch.delenv(directories.DIRECTORY_ENV_KEY, raising=False)
    monkeypatch.setattr(directories, "ENV_FILES_TO_CHECK", ())

    api = SpotiFLAC_API()
    added = api.add_registry_directory("https://example.com/dir.json")
    assert added["ok"] is True
    assert len(added["directories"]) == 1

    removed = api.remove_registry_directory("https://example.com/dir.json")
    assert removed["ok"] is True
    assert removed["directories"] == []


def test_add_registry_directory_rejects_http(tmp_path, monkeypatch):
    from SpotiFLAC.extensions import directories

    monkeypatch.setattr(
        directories, "CONFIG_FILE", tmp_path / "directory_settings.json"
    )

    api = SpotiFLAC_API()
    result = api.add_registry_directory("http://example.com/dir.json")
    assert result["ok"] is False

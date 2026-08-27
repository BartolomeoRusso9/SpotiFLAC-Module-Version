"""Tests for extensions/directories.py (registry discovery — see its module
docstring for what a "directory" is and how it differs from a registry).
"""

from __future__ import annotations

import pytest

from SpotiFLAC.extensions import directories


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Every test gets its own directory_settings.json and a clean
    environment — nothing here should read the real machine's config.
    """
    monkeypatch.setattr(
        directories, "CONFIG_FILE", tmp_path / "directory_settings.json"
    )
    monkeypatch.delenv(directories.DIRECTORY_ENV_KEY, raising=False)
    monkeypatch.setattr(directories, "ENV_FILES_TO_CHECK", ())


def test_add_list_remove_round_trip():
    assert directories.list_directory_urls() == []

    directories.add_directory("https://example.com/dir.json")
    listed = directories.list_directory_urls()
    assert len(listed) == 1
    assert listed[0]["url"] == "https://example.com/dir.json"
    assert listed[0]["enabled"] is True
    assert listed[0]["sources"] == ["custom"]

    directories.remove_directory("https://example.com/dir.json")
    assert directories.list_directory_urls() == []


def test_add_rejects_non_https():
    with pytest.raises(ValueError):
        directories.add_directory("http://example.com/dir.json")


def test_add_rejects_empty():
    with pytest.raises(ValueError):
        directories.add_directory("")


def test_env_var_directory_is_listed_and_effective(monkeypatch):
    monkeypatch.setenv(directories.DIRECTORY_ENV_KEY, "https://example.com/a.json")
    listed = directories.list_directory_urls()
    assert listed == [
        {
            "url": "https://example.com/a.json",
            "sources": ["environment"],
            "enabled": True,
        }
    ]
    assert directories.effective_directory_urls() == ["https://example.com/a.json"]


def test_removing_an_env_var_directory_disables_without_deleting(monkeypatch):
    monkeypatch.setenv(directories.DIRECTORY_ENV_KEY, "https://example.com/a.json")
    directories.remove_directory("https://example.com/a.json")

    listed = directories.list_directory_urls()
    assert listed[0]["enabled"] is False
    assert directories.effective_directory_urls() == []


def test_fetch_directory_parses_valid_listings(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/dir.json",
        json={
            "registries": [
                {
                    "name": "Example Registry",
                    "url": "https://example.com/registry.json",
                    "description": "A test registry",
                    "maintainer": "someone",
                },
            ],
        },
    )
    listings = directories.fetch_directory("https://example.com/dir.json")
    assert len(listings) == 1
    assert listings[0].name == "Example Registry"
    assert listings[0].url == "https://example.com/registry.json"


def test_fetch_directory_skips_malformed_entries(httpx_mock):
    httpx_mock.add_response(
        url="https://example.com/dir.json",
        json={
            "registries": [
                {"name": "Missing URL"},
                {"url": "https://example.com/registry.json"},  # missing name
                {
                    "name": "Valid",
                    "url": "https://example.com/valid.json",
                },
            ],
        },
    )
    listings = directories.fetch_directory("https://example.com/dir.json")
    assert len(listings) == 1
    assert listings[0].name == "Valid"


def test_fetch_all_directories_skips_unreachable_ones(httpx_mock):
    monkeypatch_urls = [
        "https://good.example.com/dir.json",
        "https://bad.example.com/dir.json",
    ]
    httpx_mock.add_response(
        url="https://good.example.com/dir.json",
        json={"registries": [{"name": "R", "url": "https://x.example.com/r.json"}]},
    )
    httpx_mock.add_exception(Exception("boom"), url="https://bad.example.com/dir.json")

    result = directories.fetch_all_directories(monkeypatch_urls)
    assert list(result.keys()) == ["https://good.example.com/dir.json"]
    assert len(result["https://good.example.com/dir.json"]) == 1


def test_probe_registry_reports_reachable(httpx_mock):
    httpx_mock.add_response(
        url="https://x.example.com/r.json",
        json={
            "extensions": [
                {
                    "id": "foo",
                    "display_name": "Foo",
                    "version": "1.0.0",
                    "description": "",
                    "download_url": "https://x.example.com/foo.spotiflac-ext",
                },
            ],
        },
    )
    health = directories.probe_registry("https://x.example.com/r.json")
    assert health.reachable is True
    assert health.extension_count == 1
    assert health.error == ""


def test_probe_registry_reports_unreachable(httpx_mock):
    httpx_mock.add_exception(Exception("connection refused"))
    health = directories.probe_registry("https://dead.example.com/r.json")
    assert health.reachable is False
    assert health.extension_count == 0
    assert health.error

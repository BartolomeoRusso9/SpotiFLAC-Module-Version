"""Cache inspection and pruning.

response_cache only checks its TTL on read, so an entry nobody asks for
again is never noticed and never deleted. These cover the sweep that does
notice, and — more importantly — what it must refuse to touch.
"""

from __future__ import annotations

import json
import time

import pytest

from SpotiFLAC.core import cache_admin


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path))
    (tmp_path / "responses" / "spotify").mkdir(parents=True)
    return tmp_path


def _write(path, payload="{}", age_days=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        import os

        os.utime(path, (old, old))
    return path


# ── stats ──────────────────────────────────────────────────────────────────


def test_stats_on_a_missing_directory_is_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "nope"))
    data = cache_admin.stats()
    assert data["exists"] is False
    assert data["entries"] == []
    assert "No cache directory" in cache_admin.format_stats(data)


def test_stats_reports_sizes_and_separates_config_from_cache(cache) -> None:
    _write(cache / "responses" / "spotify" / "a.json", "x" * 100)
    _write(cache / "isrc-cache.json", "y" * 50)
    _write(cache / "profiles.json", "z" * 10)

    data = cache_admin.stats()
    by_name = {e["name"]: e for e in data["entries"]}

    assert by_name["responses"]["prunable"] is True
    assert by_name["isrc-cache.json"]["prunable"] is True
    assert by_name["profiles.json"]["prunable"] is False, "config marked prunable"

    assert data["total_bytes"] == 160
    assert data["prunable_bytes"] == 150, "config counted as reclaimable"


def test_stats_honours_the_cache_dir_override(cache) -> None:
    assert cache_admin.stats()["root"] == str(cache)


# ── prune ──────────────────────────────────────────────────────────────────


def test_prune_removes_only_entries_older_than_the_cutoff(cache) -> None:
    fresh = _write(cache / "responses" / "spotify" / "fresh.json", "{}", age_days=0)
    stale = _write(cache / "responses" / "spotify" / "stale.json", "{}", age_days=30)

    result = cache_admin.prune(max_age_s=7 * 86400)

    assert result["removed_files"] == 1
    assert fresh.exists()
    assert not stale.exists()


def test_prune_dry_run_changes_nothing(cache) -> None:
    stale = _write(cache / "responses" / "spotify" / "stale.json", "{}", age_days=30)

    result = cache_admin.prune(max_age_s=7 * 86400, dry_run=True)

    assert result["dry_run"] is True
    assert result["removed_files"] == 1, "dry run should still report the count"
    assert stale.exists(), "dry run deleted a file"


def test_prune_never_touches_the_single_document_caches(cache) -> None:
    """Those are current or absent, not accumulations — and one of them is
    the ISRC cache, which is expensive to rebuild.
    """
    isrc = _write(cache / "isrc-cache.json", "{}", age_days=365)
    cache_admin.prune(max_age_s=1)
    assert isrc.exists()


def test_prune_leaves_no_empty_namespace_directories(cache) -> None:
    _write(cache / "responses" / "spotify" / "stale.json", "{}", age_days=30)
    cache_admin.prune(max_age_s=7 * 86400)
    assert not (cache / "responses" / "spotify").exists()


def test_prune_on_a_missing_directory_is_a_no_op(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "nope"))
    assert cache_admin.prune()["removed_files"] == 0


# ── clear ──────────────────────────────────────────────────────────────────


def test_clear_removes_caches_but_keeps_configuration(cache) -> None:
    """The important one: profiles.json and gui-settings.json sit in the same
    directory and hold things the user typed. An rmtree of the cache root
    would take them with it.
    """
    _write(cache / "responses" / "spotify" / "a.json")
    _write(cache / "isrc-cache.json")
    _write(cache / "recent-fetches.json")
    profiles = _write(cache / "profiles.json", json.dumps({"mine": {}}))
    settings = _write(cache / "gui-settings.json", "{}")

    result = cache_admin.clear()

    assert not (cache / "responses").exists()
    assert not (cache / "isrc-cache.json").exists()
    assert not (cache / "recent-fetches.json").exists()

    assert profiles.exists(), "clear() deleted the user's profiles"
    assert settings.exists(), "clear() deleted the user's GUI settings"
    assert set(result["preserved"]) == {"profiles.json", "gui-settings.json"}


def test_clear_dry_run_changes_nothing(cache) -> None:
    target = _write(cache / "isrc-cache.json")
    result = cache_admin.clear(dry_run=True)
    assert "isrc-cache.json" in result["removed"]
    assert target.exists()


def test_clear_reports_what_it_freed(cache) -> None:
    _write(cache / "isrc-cache.json", "x" * 1000)
    assert cache_admin.clear()["freed_bytes"] == 1000


# ── formatting ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024**2, "5.0 MB")],
)
def test_human_bytes(value, expected) -> None:
    assert cache_admin.human_bytes(value) == expected


def test_json_output_is_parseable(cache) -> None:
    _write(cache / "isrc-cache.json")
    assert json.loads(cache_admin.to_json(cache_admin.stats()))["exists"] is True

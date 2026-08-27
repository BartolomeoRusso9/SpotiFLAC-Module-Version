"""Tests for api_mixins/dedup.py — thin-wrapper behavior; the actual
fingerprint/grouping logic is covered by tests/test_audio_fingerprint.py.
"""

from __future__ import annotations

from SpotiFLAC.api_mixins.dedup import DedupMixin
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.webapp import ALLOWED_METHODS


def test_dedup_methods_are_web_allowed_and_resolve_to_the_mixin() -> None:
    for name in ("get_dedup_status", "scan_for_duplicates"):
        assert name in ALLOWED_METHODS
        assert name in DedupMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith("DedupMixin.")


def test_get_dedup_status_shape() -> None:
    api = SpotiFLAC_API()
    status = api.get_dedup_status()
    assert "available" in status
    assert isinstance(status["available"], bool)
    if not status["available"]:
        assert status["install_hint"]


def test_scan_for_duplicates_rejects_missing_path() -> None:
    api = SpotiFLAC_API()
    assert api.scan_for_duplicates("") == {
        "status": "error",
        "error": "No path given",
    }


def test_scan_for_duplicates_rejects_nonexistent_path() -> None:
    api = SpotiFLAC_API()
    result = api.scan_for_duplicates("/this/does/not/exist/anywhere")
    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_scan_for_duplicates_rejects_path_outside_approved_roots() -> None:
    import os
    from pathlib import Path

    if not os.path.isdir("/etc"):
        return  # no /etc on this platform (e.g. plain Windows CI) — skip
    home = Path.home().resolve()
    try:
        Path("/etc").resolve().relative_to(home)
        return  # pathological sandbox where /etc is under $HOME — skip
    except ValueError:
        pass

    api = SpotiFLAC_API()
    result = api.scan_for_duplicates("/etc")
    assert result["status"] == "error"
    assert "Access denied" in result["error"]

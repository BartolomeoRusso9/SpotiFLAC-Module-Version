"""Regression coverage for the SpotiFLAC_API mixin split (see
SpotiFLAC/api_mixins/__init__.py). Nothing here should change behavior —
only guards against a future edit accidentally dropping a method, or
breaking the MRO, when app.py / api_mixins/* are touched again.
"""

from __future__ import annotations

from SpotiFLAC.api_mixins.covers_lyrics import CoversLyricsMixin
from SpotiFLAC.api_mixins.local_tagging import LocalTaggingMixin
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.webapp import ALLOWED_METHODS


def test_api_exposes_every_web_allowed_method() -> None:
    """Every method webapp.py is willing to dispatch over HTTP must actually
    exist on the composed object — whether it's defined directly on
    SpotiFLAC_API or inherited from one of its mixins.
    """
    api = SpotiFLAC_API()
    missing = [m for m in ALLOWED_METHODS if not callable(getattr(api, m, None))]
    assert missing == []


def test_moved_methods_resolve_to_their_mixin() -> None:
    """Local-tagging and cover/lyrics methods should come from the mixin
    they were extracted into, not be silently re-defined on SpotiFLAC_API
    (which would shadow the mixin and defeat the point of the split).
    """
    for name in ("scan_local", "apply_local_tags"):
        assert name in LocalTaggingMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith(
            "LocalTaggingMixin."
        )

    for name in (
        "download_track_lyrics",
        "download_track_cover",
        "download_cover",
        "download_album_cover",
        "download_all_covers",
        "download_all_lyrics",
    ):
        assert name in CoversLyricsMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith(
            "CoversLyricsMixin."
        )


def test_moved_methods_behave_the_same_on_empty_input() -> None:
    """Cheap, synchronous edge cases that don't touch the network or spawn
    background threads — enough to prove the moved code still runs, not a
    full functional test of the local-tagging feature.
    """
    api = SpotiFLAC_API()
    assert api.scan_local("") == {"status": "error", "error": "No path given"}
    assert api.apply_local_tags([]) == {
        "status": "error",
        "error": "Nothing to apply",
    }

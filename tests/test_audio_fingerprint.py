"""Tests for core/audio_fingerprint.py.

The grouping/similarity algorithm is pure Python and tested directly with
synthetic fingerprints — no real audio file or `fpcalc` binary involved.
`is_available()`/`compute_fingerprint()`'s graceful-degradation behavior is
tested by monkeypatching, since this environment may or may not actually
have `fpcalc` installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from SpotiFLAC.core import audio_fingerprint as af


def test_identical_fingerprints_are_fully_similar():
    fp = tuple(range(100))
    assert af.fingerprint_similarity(fp, fp) == 1.0


def test_completely_different_fingerprints_score_low():
    a = tuple([0] * 50)
    b = tuple([0xFFFFFFFF] * 50)
    assert af.fingerprint_similarity(a, b) < 0.1


def test_similarity_is_robust_to_a_shifted_offset():
    """Same fingerprint, but `b` starts a few frames later than `a` (e.g.
    slightly different silence padding) — should still score as identical
    once the best alignment offset is found.
    """
    base = tuple(range(1, 101))
    a = base
    b = (9999, 9998) + base  # 2 junk frames prepended
    assert af.fingerprint_similarity(a, b) == 1.0


def test_empty_fingerprints_score_zero():
    assert af.fingerprint_similarity((), (1, 2, 3)) == 0.0
    assert af.fingerprint_similarity((1, 2, 3), ()) == 0.0
    assert af.fingerprint_similarity((), ()) == 0.0


def _fp(name: str, duration: float, raw: tuple[int, ...]) -> af.AudioFingerprint:
    return af.AudioFingerprint(path=Path(name), duration_s=duration, raw=raw)


def test_find_duplicate_groups_groups_matching_files():
    same_song = tuple(range(200))
    other_song = tuple(range(1000, 1200))

    fingerprints = [
        _fp("song_a_copy1.flac", 180.0, same_song),
        _fp("song_a_copy2.mp3", 180.2, same_song),
        _fp("song_b.flac", 210.0, other_song),
    ]

    groups = af.find_duplicate_groups(fingerprints)
    assert len(groups) == 1
    names = sorted(p.name for p in groups[0])
    assert names == ["song_a_copy1.flac", "song_a_copy2.mp3"]


def test_find_duplicate_groups_respects_duration_tolerance():
    """Two files with an identical fingerprint but wildly different
    durations should NOT be grouped — the pre-filter must actually filter,
    not just optimize.
    """
    same_raw = tuple(range(200))
    fingerprints = [
        _fp("short.flac", 30.0, same_raw),
        _fp("long.flac", 300.0, same_raw),
    ]
    groups = af.find_duplicate_groups(fingerprints, duration_tolerance_s=3.0)
    assert groups == []


def test_find_duplicate_groups_is_transitive_via_union_find():
    """A~B and B~C (each pair individually similar enough) should produce
    ONE group of three, even if A and C alone wouldn't necessarily be
    compared as a top match — this is what union-find buys over naive
    pairwise-only grouping.
    """
    common = tuple(range(200))
    fingerprints = [
        _fp("a.flac", 100.0, common),
        _fp("b.flac", 100.0, common),
        _fp("c.flac", 100.0, common),
    ]
    groups = af.find_duplicate_groups(fingerprints)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_find_duplicate_groups_returns_nothing_for_all_unique_files():
    fingerprints = [
        _fp("a.flac", 100.0, tuple(range(100))),
        _fp("b.flac", 150.0, tuple(range(1000, 1100))),
    ]
    assert af.find_duplicate_groups(fingerprints) == []


def test_is_available_false_without_fpcalc_on_path(monkeypatch):
    monkeypatch.setattr(af, "acoustid", object())  # pretend it's importable
    monkeypatch.setattr(af.shutil, "which", lambda _name: None)
    assert af.is_available() is False


def test_is_available_false_without_the_package(monkeypatch):
    monkeypatch.setattr(af, "acoustid", None)
    assert af.is_available() is False


def test_compute_fingerprint_raises_cleanly_without_acoustid(monkeypatch):
    monkeypatch.setattr(af, "acoustid", None)
    with pytest.raises(af.AudioFingerprintError):
        af.compute_fingerprint("whatever.flac")


def test_compute_fingerprint_wraps_backend_errors(monkeypatch):
    class _FakeAcoustid:
        class chromaprint:
            @staticmethod
            def decode_fingerprint(_compressed):
                raise AssertionError("should not be reached")

        @staticmethod
        def fingerprint_file(_path):
            raise RuntimeError("fpcalc exploded")

    monkeypatch.setattr(af, "acoustid", _FakeAcoustid)
    with pytest.raises(af.AudioFingerprintError, match="fpcalc exploded"):
        af.compute_fingerprint("song.flac")

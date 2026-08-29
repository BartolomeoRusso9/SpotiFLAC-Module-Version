"""Tests for core/library_upgrade.py — classifying a library against a target.

Audio fixtures are generated with ffmpeg where it is available, so the tier
classification is exercised against real container headers rather than against
a mock of mutagen. Everything that does not need a real file runs regardless.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from SpotiFLAC.core import library_upgrade as lu
from SpotiFLAC.core.library_upgrade import (
    TIER_HIRES,
    TIER_LOSSLESS,
    TIER_LOSSY,
    AudioQuality,
    UpgradeCandidate,
    scan_library,
    target_tier,
)

ffmpeg_required = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs ffmpeg to build audio fixtures"
)


def _make_audio(path, *, codec: str, rate: int = 44100, extra: list | None = None):
    """One second of a sine wave, in whatever format the test needs."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration=1:sample_rate={rate}",
        "-c:a",
        codec,
        *(extra or []),
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


# ── Tier arithmetic (no files needed) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        ("LOSSLESS", TIER_LOSSLESS),
        ("HI_RES", TIER_HIRES),
        ("HI_RES_LOSSLESS", TIER_HIRES),
        ("DOLBY_ATMOS", TIER_HIRES),
        ("HIGH", TIER_LOSSY),
        ("LOW", TIER_LOSSY),
        ("27", TIER_HIRES),
        ("6", TIER_LOSSLESS),
    ],
)
def test_target_tier_maps_canonical_qualities(quality, expected):
    assert target_tier(quality) == expected


@pytest.mark.parametrize(
    ("lossless", "rate", "depth", "expected"),
    [
        (False, 44100, 0, TIER_LOSSY),
        (False, 96000, 0, TIER_LOSSY),  # a lossy file is lossy at any rate
        (True, 44100, 16, TIER_LOSSLESS),
        (True, 48000, 16, TIER_LOSSLESS),
        (True, 96000, 24, TIER_HIRES),
        (True, 44100, 24, TIER_HIRES),  # depth alone is enough
        (True, 88200, 16, TIER_HIRES),  # so is rate
    ],
)
def test_tier_from_properties(lossless, rate, depth, expected):
    quality = AudioQuality(
        file_path="x.flac", lossless=lossless, sample_rate=rate, bits_per_sample=depth
    )
    assert quality.tier == expected


def test_describe_is_readable():
    assert (
        "24-bit"
        in AudioQuality(
            file_path="x",
            codec="flac",
            lossless=True,
            sample_rate=96000,
            bits_per_sample=24,
        ).describe()
    )
    assert (
        "320 kbps"
        in AudioQuality(
            file_path="x",
            codec="mp3",
            lossless=False,
            sample_rate=44100,
            bitrate=320000,
        ).describe()
    )


# ── Reading real files ────────────────────────────────────────────────────


def test_missing_and_unsupported_files_report_an_error(tmp_path):
    assert "not found" in lu.inspect_file(tmp_path / "nope.flac").error.lower()

    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    assert "unsupported" in lu.inspect_file(text).error.lower()


def test_a_corrupt_file_never_raises(tmp_path):
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"definitely not a flac stream")
    assert lu.inspect_file(broken).error


@ffmpeg_required
def test_cd_flac_is_lossless_tier(tmp_path):
    path = _make_audio(tmp_path / "cd.flac", codec="flac")
    quality = lu.inspect_file(path)

    assert quality.error == ""
    assert quality.lossless is True
    assert quality.tier == TIER_LOSSLESS


@ffmpeg_required
def test_hires_flac_is_hires_tier(tmp_path):
    path = _make_audio(
        tmp_path / "hr.flac", codec="flac", rate=96000, extra=["-sample_fmt", "s32"]
    )
    quality = lu.inspect_file(path)

    assert quality.sample_rate == 96000
    assert quality.tier == TIER_HIRES


@ffmpeg_required
def test_mp3_is_lossy_whatever_its_sample_rate(tmp_path):
    path = _make_audio(tmp_path / "song.mp3", codec="libmp3lame", rate=48000)
    quality = lu.inspect_file(path)

    assert quality.lossless is False
    assert quality.tier == TIER_LOSSY


@ffmpeg_required
def test_aac_in_m4a_is_lossy_but_alac_is_not(tmp_path):
    aac = _make_audio(tmp_path / "aac.m4a", codec="aac")
    assert lu.inspect_file(aac).lossless is False

    alac = _make_audio(tmp_path / "alac.m4a", codec="alac")
    assert lu.inspect_file(alac).lossless is True


# ── Scanning ──────────────────────────────────────────────────────────────


@ffmpeg_required
def test_scan_finds_only_files_below_target(tmp_path):
    _make_audio(tmp_path / "keep.flac", codec="flac")
    _make_audio(tmp_path / "upgrade.mp3", codec="libmp3lame")

    report = scan_library(tmp_path, "LOSSLESS")

    assert report.scanned == 2
    assert report.already_ok == 1
    assert [c.file_path for c in report.candidates] == [str(tmp_path / "upgrade.mp3")]
    assert "lossy" in report.candidates[0].reason


@ffmpeg_required
def test_a_lossless_file_is_a_candidate_when_the_target_is_hires(tmp_path):
    _make_audio(tmp_path / "cd.flac", codec="flac")

    report = scan_library(tmp_path, "HI_RES")

    assert len(report.candidates) == 1
    assert report.candidates[0].current_tier == TIER_LOSSLESS
    assert report.candidates[0].target_tier == TIER_HIRES


@ffmpeg_required
def test_unreadable_files_are_counted_not_fatal(tmp_path):
    _make_audio(tmp_path / "ok.mp3", codec="libmp3lame")
    (tmp_path / "broken.flac").write_bytes(b"nonsense")

    report = scan_library(tmp_path, "LOSSLESS")

    assert report.scanned == 2
    assert report.unreadable == 1
    assert len(report.candidates) == 1


@ffmpeg_required
def test_non_recursive_scan_ignores_subfolders(tmp_path):
    _make_audio(tmp_path / "top.mp3", codec="libmp3lame")
    nested = tmp_path / "album"
    nested.mkdir()
    _make_audio(nested / "deep.mp3", codec="libmp3lame")

    assert scan_library(tmp_path, "LOSSLESS").scanned == 2
    assert scan_library(tmp_path, "LOSSLESS", recursive=False).scanned == 1


@ffmpeg_required
def test_fake_hires_is_reclassified_down_when_verifying(tmp_path, monkeypatch):
    _make_audio(
        tmp_path / "fake.flac", codec="flac", rate=96000, extra=["-sample_fmt", "s32"]
    )
    monkeypatch.setattr(lu, "_verify_hires", lambda _path: "fake_hires")

    report = scan_library(tmp_path, "HI_RES", verify_hires=True)

    assert len(report.candidates) == 1
    assert "declares Hi-Res" in report.candidates[0].reason
    # Without verification the same file passes.
    assert scan_library(tmp_path, "HI_RES").candidates == []


@ffmpeg_required
def test_progress_callback_failure_does_not_abort_the_scan(tmp_path):
    _make_audio(tmp_path / "a.mp3", codec="libmp3lame")

    def broken(_done, _total, _path):
        raise RuntimeError("UI went away")

    report = scan_library(tmp_path, "LOSSLESS", progress=broken)
    assert report.scanned == 1


def test_scanning_a_missing_path_reports_nothing_rather_than_raising(tmp_path):
    report = scan_library(tmp_path / "does-not-exist", "LOSSLESS")
    assert report.scanned == 0
    assert report.candidates == []


# ── Resolving candidates to URLs ──────────────────────────────────────────


class FakeSearchClient:
    def __init__(self, by_query: dict) -> None:
        self.by_query = by_query
        self.queries: list[str] = []

    async def search_async(self, query: str, limit: int = 20) -> dict:
        self.queries.append(query)
        return {"tracks": self.by_query.get(query, [])}


class FakeTrack:
    def __init__(self, track_id: str, url: str = "") -> None:
        self.id = track_id
        self.external_url = url


def _candidate(**kwargs) -> UpgradeCandidate:
    base = {
        "file_path": "/music/x.mp3",
        "quality": AudioQuality(file_path="/music/x.mp3"),
        "title": "Song",
        "artist": "Artist",
    }
    base.update(kwargs)
    return UpgradeCandidate(**base)


def test_isrc_is_preferred_over_text_search():
    client = FakeSearchClient(
        {"isrc:ITAAA0000001": [FakeTrack("t1", "https://open.spotify.com/track/t1")]}
    )
    candidate = _candidate(isrc="ITAAA0000001")

    url = asyncio.run(lu.resolve_candidate_async(candidate, client=client))

    assert url == "https://open.spotify.com/track/t1"
    assert client.queries == ["isrc:ITAAA0000001"]


def test_text_search_is_the_fallback_without_an_isrc():
    client = FakeSearchClient({"Artist Song": [FakeTrack("t2")]})

    url = asyncio.run(lu.resolve_candidate_async(_candidate(), client=client))

    assert url == "https://open.spotify.com/track/t2"


def test_isrc_miss_falls_through_to_text_search():
    client = FakeSearchClient({"Artist Song": [FakeTrack("t3")]})
    candidate = _candidate(isrc="NOPE")

    url = asyncio.run(lu.resolve_candidate_async(candidate, client=client))

    assert url.endswith("/track/t3")
    assert client.queries == ["isrc:NOPE", "Artist Song"]


def test_an_untaggable_file_resolves_to_nothing():
    candidate = _candidate(title="", artist="")
    url = asyncio.run(
        lu.resolve_candidate_async(candidate, client=FakeSearchClient({}))
    )
    assert url == ""


def test_plan_keeps_unresolved_candidates_visible():
    client = FakeSearchClient({"Artist Song": [FakeTrack("t1")]})
    report = lu.ScanReport(target="LOSSLESS")
    report.candidates = [_candidate(), _candidate(title="Unknown", artist="Nobody")]

    pairs = asyncio.run(lu.plan_async(report, client=client))

    assert len(pairs) == 2
    assert pairs[0][1].endswith("/track/t1")
    assert pairs[1][1] == ""

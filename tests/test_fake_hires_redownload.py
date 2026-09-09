"""`--redownload-fake-hires`: replacing an upsampled Hi-Res file.

`--verify-hires` alone only warns. With the redownload flag the flagged
file is set aside, the track is fetched again at LOSSLESS, and the flagged
file is deleted only once the replacement is actually on disk — the point
being that a *heuristic* must never be able to leave the user with no file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from SpotiFLAC import downloader as dl
from SpotiFLAC.core.hires_check import HiResCheckResult
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

TRACK_FILE = "Song - Artist.flac"
QUARANTINED = TRACK_FILE + dl._FAKE_HIRES_QUARANTINE_SUFFIX


def _track() -> TrackMetadata:
    return TrackMetadata(
        id="0000000000000000000000",
        title="Song",
        artists="Artist",
        artist_names=["Artist"],
        album="Album",
        album_artist="Artist",
        isrc="",
        duration_ms=200000,
        is_explicit=False,
    )


def _verdict(verdict: str, sample_rate: int) -> HiResCheckResult:
    return HiResCheckResult(
        file_path=TRACK_FILE,
        declared_sample_rate=sample_rate,
        total_duration_s=200.0,
        analyzed_duration_s=30.0,
        cutoff_frequency_hz=21800.0 if verdict == "fake_hires" else 45000.0,
        noise_floor_db=-80.0,
        verdict=verdict,
        reason=(
            f"declares {sample_rate} Hz but content stops at ~21800 Hz"
            if verdict == "fake_hires"
            else ""
        ),
    )


class _Provider:
    """Writes one file per attempt and records the quality it was asked for."""

    def __init__(self, tmp_path: Path, fail_after_first: bool = False) -> None:
        self.name = "tidal"
        self._dir = tmp_path
        self._fail_after_first = fail_after_first
        self.qualities: list[str] = []

    def set_progress_callback(self, cb) -> None:
        pass

    async def download_track_async(self, metadata, output_dir, **kwargs):
        self.qualities.append(kwargs.get("quality", ""))
        if self._fail_after_first and len(self.qualities) > 1:
            return DownloadResult.fail(self.name, "provider is down")
        path = self._dir / TRACK_FILE
        path.write_bytes(b"audio-" + self.qualities[-1].encode())
        return DownloadResult.ok(self.name, str(path))


@pytest.fixture
def _offline(monkeypatch):
    """No ISRC lookups, and no background check racing the inline one."""

    async def no_lookup(isrc):
        return None

    monkeypatch.setattr("SpotiFLAC.core.recording_guard._lookup_isrc_async", no_lookup)
    monkeypatch.setattr(dl, "_schedule_hires_check", lambda *a, **k: None)


def _stub_analysis(monkeypatch, *verdicts: HiResCheckResult | None) -> list[str]:
    """Feeds canned verdicts to the inline check and records what it saw."""
    seen: list[str] = []
    queue = list(verdicts)

    async def fake_analyze(file_path: str):
        seen.append(file_path)
        return queue.pop(0) if queue else None

    monkeypatch.setattr(dl, "_analyze_hires_async", fake_analyze)
    return seen


def _run(providers, tmp_path, track, **opt_kwargs):
    opts = dl.DownloadOptions(
        output_dir=str(tmp_path),
        embed_lyrics=False,
        enrich_metadata=False,
        **{"quality": "HI_RES_LOSSLESS", **opt_kwargs},
    )
    return asyncio.run(
        dl.download_one_async(track, str(tmp_path), providers, opts),
    )


def test_a_fake_hires_file_is_replaced_by_a_lossless_one(
    _offline, monkeypatch, tmp_path
) -> None:
    _stub_analysis(monkeypatch, _verdict("fake_hires", 96000))
    provider = _Provider(tmp_path)

    result = _run([provider], tmp_path, _track(), redownload_fake_hires=True)

    assert result.success
    assert result.file_path == str(tmp_path / TRACK_FILE)
    assert provider.qualities == ["HI_RES_LOSSLESS", "LOSSLESS"]
    # The replacement is what is left on disk, under the original name.
    assert (tmp_path / TRACK_FILE).read_bytes() == b"audio-LOSSLESS"
    assert not (tmp_path / QUARANTINED).exists()


def test_a_genuine_hires_file_is_left_alone(_offline, monkeypatch, tmp_path) -> None:
    _stub_analysis(monkeypatch, _verdict("genuine_hires", 96000))
    provider = _Provider(tmp_path)

    result = _run([provider], tmp_path, _track(), redownload_fake_hires=True)

    assert result.success
    assert provider.qualities == ["HI_RES_LOSSLESS"]
    assert (tmp_path / TRACK_FILE).read_bytes() == b"audio-HI_RES_LOSSLESS"


def test_an_unverifiable_file_is_left_alone(_offline, monkeypatch, tmp_path) -> None:
    """librosa missing, file unreadable, analysis error — all report None."""
    _stub_analysis(monkeypatch, None)
    provider = _Provider(tmp_path)

    result = _run([provider], tmp_path, _track(), redownload_fake_hires=True)

    assert result.success
    assert provider.qualities == ["HI_RES_LOSSLESS"]
    assert (tmp_path / TRACK_FILE).exists()


def test_a_failed_replacement_puts_the_flagged_file_back(
    _offline, monkeypatch, tmp_path
) -> None:
    """The whole reason the flagged file is renamed instead of deleted."""
    _stub_analysis(monkeypatch, _verdict("fake_hires", 96000))
    provider = _Provider(tmp_path, fail_after_first=True)

    result = _run([provider], tmp_path, _track(), redownload_fake_hires=True)

    assert result.success
    assert result.file_path == str(tmp_path / TRACK_FILE)
    assert (tmp_path / TRACK_FILE).read_bytes() == b"audio-HI_RES_LOSSLESS"
    assert not (tmp_path / QUARANTINED).exists()


def test_a_lossless_request_is_never_a_fake(_offline, monkeypatch, tmp_path) -> None:
    """CD-range audio from a LOSSLESS request is the request, not a finding."""
    seen = _stub_analysis(monkeypatch, _verdict("fake_hires", 96000))
    provider = _Provider(tmp_path)

    result = _run(
        [provider],
        tmp_path,
        _track(),
        quality="LOSSLESS",
        redownload_fake_hires=True,
    )

    assert result.success
    assert seen == []
    assert provider.qualities == ["LOSSLESS"]


def test_verify_alone_never_touches_the_file(_offline, monkeypatch, tmp_path) -> None:
    seen = _stub_analysis(monkeypatch, _verdict("fake_hires", 96000))
    provider = _Provider(tmp_path)

    result = _run([provider], tmp_path, _track(), verify_hires=True)

    assert result.success
    # The inline path is the redownload path; report-only stays in the
    # background task, which this fixture stubs out.
    assert seen == []
    assert provider.qualities == ["HI_RES_LOSSLESS"]
    assert (tmp_path / TRACK_FILE).read_bytes() == b"audio-HI_RES_LOSSLESS"


def test_the_kept_transcode_source_is_set_aside_as_well(
    _offline, monkeypatch, tmp_path
) -> None:
    """With transcode_keep_original the provider's own file survives the
    conversion under the same stem. Left in place while the replacement is
    fetched, BaseProvider._file_exists() finds it and reports the track as
    already downloaded — so the replacement never happens and the flagged
    file comes back.
    """
    _stub_analysis(monkeypatch, _verdict("fake_hires", 96000))
    provider = _Provider(tmp_path)
    kept_source = tmp_path / (Path(TRACK_FILE).stem + ".m4a")
    kept_source.write_bytes(b"provider-source")

    opts = dl.DownloadOptions(
        output_dir=str(tmp_path),
        embed_lyrics=False,
        enrich_metadata=False,
        quality="HI_RES_LOSSLESS",
        redownload_fake_hires=True,
        transcode_keep_original=True,
        transcode_to="flac",
    )
    result = asyncio.run(
        dl.download_one_async(_track(), str(tmp_path), [provider], opts),
    )

    assert result.success
    assert provider.qualities == ["HI_RES_LOSSLESS", "LOSSLESS"]
    # Both the flagged file and the source it was made from are gone.
    assert not kept_source.exists()
    assert not (tmp_path / (QUARANTINED)).exists()
    assert not (
        tmp_path / (kept_source.name + dl._FAKE_HIRES_QUARANTINE_SUFFIX)
    ).exists()


def test_a_kept_source_is_put_back_when_the_replacement_fails(
    _offline, monkeypatch, tmp_path
) -> None:
    """Both files move, so both must come back."""
    _stub_analysis(monkeypatch, _verdict("fake_hires", 96000))
    provider = _Provider(tmp_path, fail_after_first=True)
    kept_source = tmp_path / (Path(TRACK_FILE).stem + ".m4a")
    kept_source.write_bytes(b"provider-source")

    opts = dl.DownloadOptions(
        output_dir=str(tmp_path),
        embed_lyrics=False,
        enrich_metadata=False,
        quality="HI_RES_LOSSLESS",
        redownload_fake_hires=True,
        transcode_keep_original=True,
        transcode_to="flac",
    )
    result = asyncio.run(
        dl.download_one_async(_track(), str(tmp_path), [provider], opts),
    )

    assert result.success
    assert (tmp_path / TRACK_FILE).read_bytes() == b"audio-HI_RES_LOSSLESS"
    assert kept_source.read_bytes() == b"provider-source"


def test_asking_for_the_replacement_turns_the_check_on(tmp_path) -> None:
    opts = dl.DownloadOptions(output_dir=str(tmp_path), redownload_fake_hires=True)
    assert opts.verify_hires

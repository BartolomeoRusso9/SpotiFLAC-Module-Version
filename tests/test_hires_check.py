"""The spectral checker itself, which had no tests of its own.

Signals are synthesised rather than fixtured: a "fake Hi-Res" file is
exactly a band-limited 44.1 kHz signal resampled up, which numpy and
librosa can build in a few lines and which no committed binary could
describe as clearly.
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf

from SpotiFLAC.core.hires_check import check_file

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="the ffmpeg decode fallback needs ffmpeg and ffprobe on PATH",
)

HIRES_SR = 176400
CD_SR = 44100
SECONDS = 4


def _noise(sample_rate: int):
    """Full-bandwidth noise: content right up to the file's own Nyquist."""
    rng = np.random.default_rng(0)
    return rng.standard_normal(sample_rate * SECONDS).astype(np.float32) * 0.2


def _write(path, data, sample_rate: int, subtype="PCM_24") -> str:
    sf.write(str(path), data, sample_rate, format="FLAC", subtype=subtype)
    return str(path)


def _upsampled_from_cd():
    """CD-bandwidth noise carried in a 176.4 kHz container: a fake Hi-Res.

    Built in the frequency domain rather than by resampling, so the test
    owns the thing it is asserting about. Everything above 22.05 kHz is
    zeroed, then a short raised-cosine taper is left running up to ~24.5
    kHz — the transition-band tail a real resampler leaves behind, and the
    reason the cutoff threshold sits at 28 kHz rather than hugging 22.05.
    """
    y = _noise(HIRES_SR)
    spectrum = np.fft.rfft(y)
    freqs = np.fft.rfftfreq(len(y), 1.0 / HIRES_SR)

    taper_end = 24_500.0
    cd_nyquist = CD_SR / 2
    in_taper = (freqs >= cd_nyquist) & (freqs < taper_end)
    ramp = (freqs[in_taper] - cd_nyquist) / (taper_end - cd_nyquist)
    spectrum[in_taper] *= 0.5 * (1 + np.cos(np.pi * ramp)) * 1e-3
    spectrum[freqs >= taper_end] = 0

    return np.fft.irfft(spectrum, n=len(y)).astype(np.float32)


def _pcm(bits: int, frames: int):
    """Noise occupying exactly `bits` bits, left-justified in an int32.

    That justification is soundfile's own convention for every PCM subtype,
    so a 16-bit signal written into a 24-bit container arrives with its low
    8 bits zero — which is precisely what a padded CD master looks like.
    """
    rng = np.random.default_rng(1)
    return (
        rng.integers(-(2 ** (bits - 1)), 2 ** (bits - 1), frames) << (32 - bits)
    ).astype(np.int32)


def test_genuine_hires_is_not_flagged(tmp_path) -> None:
    path = _write(tmp_path / "genuine.flac", _noise(HIRES_SR), HIRES_SR)
    assert check_file(path).verdict == "genuine_hires"


def test_an_upsampled_cd_signal_is_flagged(tmp_path) -> None:
    """The case the checker exists for, and the one it used to pass.

    Resampling 44.1 kHz up to 176.4 kHz adds no content above the CD
    Nyquist — only the resampler's own transition-band tail, which lands
    a couple of kHz above 22.05 kHz and is what the old 24 kHz threshold
    mistook for real Hi-Res content.
    """
    path = _write(tmp_path / "fake.flac", _upsampled_from_cd(), HIRES_SR)

    result = check_file(path)
    assert result.verdict == "fake_hires"
    assert result.is_suspicious
    # Well below the file's own 88.2 kHz Nyquist, and above 22.05 kHz.
    assert 22_000 < result.cutoff_frequency_hz < 28_000


def test_a_cd_file_claims_nothing_and_is_not_flagged(tmp_path) -> None:
    path = _write(tmp_path / "cd.flac", _noise(CD_SR), CD_SR, subtype="PCM_16")
    result = check_file(path)
    assert result.verdict == "standard_definition"
    assert not result.is_suspicious


def test_a_padded_24_bit_cd_master_is_flagged(tmp_path) -> None:
    """The 24-bit/44.1 kHz case, which no spectral test can judge.

    Such a file claims Hi-Res by depth alone — its sample rate claims
    nothing, so its CD-range cutoff is the correct answer rather than a
    finding. What gives it away is that the bottom 8 of its 24 bits are
    zero in every sample: a 16-bit master padded out.
    """
    path = _write(tmp_path / "padded.flac", _pcm(16, CD_SR * SECONDS), CD_SR)

    result = check_file(path)
    assert result.verdict == "fake_hires"
    assert result.declared_bit_depth == 24
    assert result.effective_bit_depth == 16
    assert result.padded_bit_depth
    assert "24-bit" in result.reason and "16 bits" in result.reason
    # The spectral half made no finding, so it must not appear in the reason.
    assert "content stops" not in result.reason


def test_a_real_24_bit_cd_rate_file_is_not_flagged(tmp_path) -> None:
    """The other side of it: 24-bit/44.1 kHz is a modest but real Hi-Res
    tier, and a file whose 24 bits genuinely carry data must survive it.
    """
    path = _write(tmp_path / "real24.flac", _pcm(24, CD_SR * SECONDS), CD_SR)

    result = check_file(path)
    assert result.verdict == "genuine_hires"
    assert (result.declared_bit_depth, result.effective_bit_depth) == (24, 24)
    assert not result.padded_bit_depth


def test_16_bit_makes_no_claim_for_the_depth_test_to_answer(tmp_path) -> None:
    """A 16-bit container declares no Hi-Res depth, so the depth test stands
    down and the spectral verdict is the whole answer — here, a genuine
    176.4 kHz file that happens to be 16-bit."""
    path = _write(tmp_path / "hires16.flac", _noise(HIRES_SR), HIRES_SR, "PCM_16")
    result = check_file(path)
    assert result.declared_bit_depth == 16
    assert not result.padded_bit_depth
    assert result.verdict == "genuine_hires"


def test_silence_is_inconclusive_rather_than_guessed(tmp_path) -> None:
    path = _write(
        tmp_path / "silent.flac", np.zeros(HIRES_SR * SECONDS, np.float32), HIRES_SR
    )
    assert check_file(path).verdict == "inconclusive"


def test_a_lower_noise_floor_still_measures_the_spectrum(tmp_path) -> None:
    """librosa's amplitude_to_db clamps at peak-80 dB by default, which is
    also where noise_floor_db sits. Every clamped bin therefore compared as
    "active" against any floor below -80, and the cutoff came back as the
    file's full Nyquist frequency — for every file, whatever it contained,
    silently disabling a parameter documented as tunable.

    Only -90 dB is asserted on. Below about -100 dB the 24-bit quantisation
    floor of the file itself is real broadband content, so a cutoff at
    Nyquist there is the right answer rather than the bug.
    """
    path = _write(tmp_path / "fake.flac", _upsampled_from_cd(), HIRES_SR)

    result = check_file(path, noise_floor_db=-90.0)
    assert result.cutoff_frequency_hz < HIRES_SR / 2, (
        "a floor below -80 dB reported the full Nyquist as content"
    )


# ── Formats libsndfile cannot open ────────────────────────────────────────
#
# ALAC/MP4, WavPack and TTA are three of `--transcode`'s seven targets, and
# libsndfile opens none of them. Only the *lossy* targets are deliberately
# skipped by the download path, so before the ffmpeg fallback existed
# `--transcode alac --verify-hires` looked enabled while checking nothing.


def _transcode(src, dest, codec) -> str:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-c:a", codec, str(dest), "-y"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return str(dest)


@needs_ffmpeg
@pytest.mark.parametrize(
    ("codec", "suffix"),
    [("alac", ".m4a"), ("wavpack", ".wv"), ("tta", ".tta")],
)
def test_a_padded_depth_is_caught_through_ffmpeg(tmp_path, codec, suffix) -> None:
    """The finding has to survive the container, not just the codec."""
    src = _write(tmp_path / "src.flac", _pcm(16, CD_SR * SECONDS), CD_SR)
    converted = _transcode(src, tmp_path / f"converted{suffix}", codec)

    result = check_file(converted)
    assert result.verdict == "fake_hires"
    assert result.effective_bit_depth == 16
    assert result.declared_bit_depth > 16


@needs_ffmpeg
@pytest.mark.parametrize(
    ("codec", "suffix"),
    [("alac", ".m4a"), ("wavpack", ".wv"), ("tta", ".tta")],
)
def test_a_genuine_file_survives_those_containers(tmp_path, codec, suffix) -> None:
    """The other half: the fallback must not invent findings either."""
    src = _write(tmp_path / "src.flac", _noise(HIRES_SR), HIRES_SR)
    converted = _transcode(src, tmp_path / f"converted{suffix}", codec)

    assert check_file(converted).verdict == "genuine_hires"


@needs_ffmpeg
def test_ffmpeg_and_libsndfile_agree_on_a_file_both_can_read(tmp_path) -> None:
    """A FLAC goes through soundfile; the same audio in an MP4 goes through
    ffmpeg. Two decoders, one verdict — otherwise the fallback would be
    quietly measuring something else.
    """
    src = _write(tmp_path / "src.flac", _noise(HIRES_SR), HIRES_SR)
    converted = _transcode(src, tmp_path / "converted.m4a", "alac")

    native = check_file(src)
    fallback = check_file(converted)
    assert native.verdict == fallback.verdict
    assert native.declared_sample_rate == fallback.declared_sample_rate
    assert abs(native.cutoff_frequency_hz - fallback.cutoff_frequency_hz) < 200


def test_an_unreadable_format_says_which_half_is_missing(tmp_path, monkeypatch) -> None:
    """Without ffmpeg the error names the real problem instead of calling a
    perfectly good file corrupt."""
    from SpotiFLAC.core import hires_check

    monkeypatch.setattr(hires_check, "_ffmpeg_available", lambda: False)
    path = tmp_path / "not-audio.m4a"
    path.write_bytes(b"\x00" * 4096)

    with pytest.raises(hires_check.HiResCheckError) as excinfo:
        check_file(path)
    assert "ffmpeg" in str(excinfo.value)

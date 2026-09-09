"""The spectral checker itself, which had no tests of its own.

Signals are synthesised rather than fixtured: a "fake Hi-Res" file is
exactly a band-limited 44.1 kHz signal resampled up, which numpy and
librosa can build in a few lines and which no committed binary could
describe as clearly.
"""

from __future__ import annotations

import numpy as np
import pytest

librosa = pytest.importorskip("librosa")
sf = pytest.importorskip("soundfile")

from SpotiFLAC.core.hires_check import check_file  # noqa: E402

HIRES_SR = 176400
CD_SR = 44100
SECONDS = 4


def _noise(sample_rate: int) -> np.ndarray:
    """Full-bandwidth noise: content right up to the file's own Nyquist."""
    rng = np.random.default_rng(0)
    return rng.standard_normal(sample_rate * SECONDS).astype(np.float32) * 0.2


def _write(path, data: np.ndarray, sample_rate: int) -> str:
    sf.write(str(path), data, sample_rate, format="FLAC", subtype="PCM_24")
    return str(path)


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
    cd = _noise(CD_SR)
    upsampled = librosa.resample(cd, orig_sr=CD_SR, target_sr=HIRES_SR)
    path = _write(tmp_path / "fake.flac", upsampled, HIRES_SR)

    result = check_file(path)
    assert result.verdict == "fake_hires"
    assert result.is_suspicious
    # Well below the file's own 88.2 kHz Nyquist, and above 22.05 kHz.
    assert 22_000 < result.cutoff_frequency_hz < 28_000


def test_a_cd_file_claims_nothing_and_is_not_flagged(tmp_path) -> None:
    path = _write(tmp_path / "cd.flac", _noise(CD_SR), CD_SR)
    result = check_file(path)
    assert result.verdict == "standard_definition"
    assert not result.is_suspicious


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
    cd = _noise(CD_SR)
    upsampled = librosa.resample(cd, orig_sr=CD_SR, target_sr=HIRES_SR)
    path = _write(tmp_path / "fake.flac", upsampled, HIRES_SR)

    result = check_file(path, noise_floor_db=-90.0)
    assert result.cutoff_frequency_hz < HIRES_SR / 2, (
        "a floor below -80 dB reported the full Nyquist as content"
    )

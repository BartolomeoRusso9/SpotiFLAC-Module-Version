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
    # Loud from start to end: the floor cannot be read, so a genuine master
    # filtered at 22 kHz would look the same. Flagged, never replaced.
    assert result.brickwall_hz == 22050
    assert result.noise_floor_class == "masked"
    assert result.confidence == "suspect"
    assert not result.redownload_safe


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
    assert result.confidence == "certain"
    assert result.redownload_safe


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
    assert (
        result.cutoff_frequency_hz < HIRES_SR / 2
    ), "a floor below -80 dB reported the full Nyquist as content"


# ── Telling a fake from a filtered master ─────────────────────────────────
#
# Above 22 kHz an upsampled CD and a master low-pass filtered in mastering
# are the same signal. What separates them — and decides whether a LOSSLESS
# copy would lose anything — is how the file was made: exact upsampling
# fingerprints, a resampler's cliff, and whether the in-band noise floor ever
# drops below 16-bit quantization noise. Mirrors go_backend/hires_check_test.go.

CD_SOURCE_FRAMES = 2**17  # ~3 s at 44.1 kHz, x4 = 176.4 kHz


def _ideal_upsample(x, ratio: int):
    """Band-limited interpolation: nothing at all above the source Nyquist."""
    return np.fft.irfft(np.fft.rfft(x), n=len(x) * ratio) * ratio


def _source_with_quiet_half(quiet_noise: float):
    """Loud noise, then a half holding only `quiet_noise`: the gaps real
    music leaves, where a noise floor shows through."""
    rng = np.random.default_rng(3)
    y = rng.standard_normal(CD_SOURCE_FRAMES)
    y[: CD_SOURCE_FRAMES // 2] *= 0.2
    y[CD_SOURCE_FRAMES // 2 :] *= quiet_noise
    return y


def _dither_to_16_bit(y):
    """What a CD master is: TPDF-dithered 16-bit, as floats."""
    rng = np.random.default_rng(4)
    dither = (rng.random(len(y)) - rng.random(len(y))) / 32768
    return np.round((y + dither) * 32768) / 32768


def _write_float24(path, y, sample_rate: int) -> str:
    return _write(path, np.clip(y, -1, 1 - 2**-23).astype(np.float64), sample_rate)


def test_an_upsampled_16_bit_master_is_likely_and_replaceable(tmp_path) -> None:
    """The cliff sits at 22.05 kHz and the quiet half shows 16-bit dither
    noise: a LOSSLESS copy holds everything this file does."""
    signal = _ideal_upsample(_dither_to_16_bit(_source_with_quiet_half(0.0)), 4)
    result = check_file(_write_float24(tmp_path / "cd16.flac", signal, HIRES_SR))

    assert result.verdict == "fake_hires"
    assert result.brickwall_hz == 22050
    assert result.noise_floor_class == "at_16bit"
    assert result.confidence == "likely"
    assert result.redownload_safe


def test_a_24_bit_master_made_at_44k_is_only_suspect(tmp_path) -> None:
    """The same cliff, but the quiet half holds detail far below 16-bit
    noise: LOSSLESS would lose that depth, so it is never replaced."""
    signal = _ideal_upsample(_source_with_quiet_half(1e-6), 4)
    result = check_file(_write_float24(tmp_path / "master24.flac", signal, HIRES_SR))

    assert result.verdict == "fake_hires"
    assert result.noise_floor_class == "below_16bit"
    assert result.confidence == "suspect"
    assert not result.redownload_safe
    assert "genuine master" in result.reason


def test_a_gradual_mastering_roll_off_is_only_suspect(tmp_path) -> None:
    """Ends below 28 kHz, but with no resampler's cliff anywhere.

    Faded in and out: an abrupt start inside the analyzed window is a step,
    and a step's broadband splatter would be measured as content.
    """
    y = _noise(HIRES_SR)
    spectrum = np.fft.rfft(y)
    freqs = np.fft.rfftfreq(len(y), 1.0 / HIRES_SR)
    gain_db = np.where(freqs > 16000, -10.0 * (freqs - 16000) / 1000, 0.0)
    signal = np.fft.irfft(spectrum * 10 ** (gain_db / 20), n=len(y))
    fade = np.hanning(4096)
    signal[:2048] *= fade[:2048]
    signal[-2048:] *= fade[2048:]
    result = check_file(_write_float24(tmp_path / "lpf.flac", signal, HIRES_SR))

    assert result.verdict == "fake_hires"
    assert result.brickwall_hz == 0
    assert result.confidence == "suspect"


def _cheap_upsamples():
    """24-bit source samples (so the depth test passes) taken to 176.4 kHz
    by the cheap upsamplers whose output is an exact, checkable pattern."""
    rng = np.random.default_rng(1)
    source = rng.integers(-(2**23), 2**23, CD_SOURCE_FRAMES + 1).astype(np.int64)
    a, b = source[:-1], source[1:]
    steps = np.arange(4)[None, :] / 4
    hold = np.repeat(a, 4)
    line = np.round(a[:, None] + (b - a)[:, None] * steps).ravel().astype(np.int64)
    zero_stuffed = np.zeros(len(a) * 4, dtype=np.int64)
    zero_stuffed[::4] = a // 2
    return {"sample_hold": hold, "linear_interpolation": line, "imaging": zero_stuffed}


@pytest.mark.parametrize("artifact", ["sample_hold", "linear_interpolation", "imaging"])
def test_integer_upsampling_artifacts_without_a_measurable_floor_are_suspect(
    tmp_path, artifact
) -> None:
    samples = (_cheap_upsamples()[artifact] << 8).astype(np.int32)
    result = check_file(_write(tmp_path / f"{artifact}.flac", samples, HIRES_SR))

    assert result.upsampling_artifact == artifact
    assert result.verdict == "fake_hires"
    assert result.noise_floor_class == ""
    assert result.confidence == "suspect"
    assert not result.redownload_safe


def test_genuine_audio_shows_no_evidence_at_all(tmp_path) -> None:
    rng = np.random.default_rng(0)
    stereo = (rng.standard_normal((HIRES_SR * SECONDS, 2)) * 0.2).astype(np.float32)
    result = check_file(_write(tmp_path / "genuine.flac", stereo, HIRES_SR))

    assert result.verdict == "genuine_hires"
    assert result.upsampling_artifact == ""
    assert result.brickwall_hz == 0
    assert result.confidence == ""


def test_music_is_told_apart_from_ultrasonic_noise(tmp_path) -> None:
    """A DSD or analog-tape transfer: music that stops around 30 kHz, and a
    steady ultrasonic noise hump running on to ~80 kHz. The active-content
    cutoff marks the end of the hump; the music bandwidth must not, and the
    useful rate is the 88.2 kHz that holds all the music."""
    n = HIRES_SR * SECONDS
    freqs = np.fft.rfftfreq(n, 1.0 / HIRES_SR)

    def band(seed: int, low: float, high: float):
        spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
        spectrum[(freqs < low) | (freqs > high)] = 0
        return np.fft.irfft(spectrum, n=n)

    music, hump = band(5, 20, 30000), band(6, 40000, 80000)
    # A 3 Hz swell swings the music ~20 dB; the hump never moves.
    swell = 0.55 + 0.45 * np.cos(2 * np.pi * 3 * np.arange(n) / HIRES_SR)
    signal = 0.2 * music * swell + 0.004 * hump
    fade = np.hanning(4096)
    signal[:2048] *= fade[:2048]
    signal[-2048:] *= fade[2048:]
    result = check_file(_write_float24(tmp_path / "dsd.flac", signal, HIRES_SR))

    assert result.verdict == "genuine_hires"
    assert result.cutoff_frequency_hz > 70000
    assert 28000 <= result.music_cutoff_hz <= 33000
    assert result.ultrasonic_noise_only
    assert result.useful_sample_rate == 88200


# A real resampler rather than an ideal one: ffmpeg's leaves a transition
# band and, by default, a stopband plateau ~50 dB down that the cutoff test
# alone reads as content up to Nyquist.
_QUIET_EVERY_8S = "gt(mod(t\\,8)\\,6)"


def _ffmpeg_upsampled(tmp_path, name: str, source_chain: str) -> str:
    dest = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=r=44100:c=pink:a=0.3:d=16,"
            f"aformat=channel_layouts=stereo,{source_chain}",
            "-af",
            "aresample=96000",
            "-c:a",
            "flac",
            "-sample_fmt",
            "s32",
            "-bits_per_raw_sample",
            "24",
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return str(dest)


@needs_ffmpeg
def test_ffmpeg_upsampled_cd_with_quiet_passages_is_likely(tmp_path) -> None:
    path = _ffmpeg_upsampled(
        tmp_path,
        "cd16.flac",
        f"volume=0:enable='{_QUIET_EVERY_8S}',"
        "aresample=osf=s16:dither_method=triangular",
    )
    result = check_file(path)
    assert result.confidence == "likely"
    assert result.brickwall_hz == 22050


@needs_ffmpeg
def test_ffmpeg_upsampled_24_bit_master_is_only_suspect(tmp_path) -> None:
    path = _ffmpeg_upsampled(
        tmp_path,
        "master24.flac",
        f"volume=0.00001:enable='{_QUIET_EVERY_8S}',aresample=osf=s32",
    )
    result = check_file(path)
    assert result.confidence == "suspect"
    assert result.noise_floor_class == "below_16bit"


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

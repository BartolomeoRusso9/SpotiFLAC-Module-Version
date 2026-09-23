"""Hi-Res authenticity checker.

Detects "fake hi-res" audio files. A file can claim Hi-Res along two
independent axes, and each is checked on its own terms:

  Sample rate — it declares 96 kHz but its spectral content stops at, or
    just above, the ~22.05 kHz Nyquist limit of a 44.1 kHz source. That
    sharp cutoff is the fingerprint of upsampling: taking a CD-quality or
    lossy source and re-encoding it at a higher rate without adding any
    real high-frequency content. Measuring it is a heuristic.

  Bit depth — it declares 24-bit but only 16 of those bits ever carry
    data, the low 8 being zero in every single sample. That is a CD master
    padded out to look deeper, and unlike the spectral test this one is
    exact: bits are either used or they are not. It is also the only test
    that says anything about a 24-bit/44.1 kHz file, which claims Hi-Res
    purely by depth and which the spectral test cannot judge at all.

The cutoff alone cannot tell the two ways a spectrum ends at 22 kHz apart:
an upsampled CD, and a genuine hi-res master that was deliberately low-pass
filtered during mastering. Above 22 kHz they are the same signal. So every
"fake_hires" verdict is graded by evidence that can separate them, and that
answers the question that actually matters before replacing a file: would a
LOSSLESS copy lose anything this one holds?

  certain — an exact fingerprint: padded bit depth, every sample repeated
    (sample-and-hold), in-between samples on a straight line (linear
    interpolation), or the band above 22 kHz mirroring the audible one
    (imaging).
  likely  — a resampler's cliff right at 22.05/24 kHz (flat passband, then
    40+ dB down within a few kHz), over an in-band noise floor no lower
    than 16-bit quantization noise: nothing in the file exceeds what a
    16-bit LOSSLESS copy holds.
  suspect — anything else that fails the cutoff test: a gradual roll-off,
    a floor below the 16-bit level (real resolution a 16-bit copy would
    lose), or a floor the music never leaves quiet enough to read. It may
    be a genuine master and is never replaced automatically
    (`redownload_safe` is False).

This is kept in lockstep with SpotiFLAC-Mobile's go_backend/hires_check.go
and hires_check_evidence.go: same thresholds, same verdicts.

Public API:
    - is_available() -> bool
    - check_file(path, ...) -> HiResCheckResult          (sync, blocking)
    - check_file_async(path, ...) -> HiResCheckResult     (off-thread)
    - HiResCheckError                                      (raised on failure)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("SpotiFLAC.hires_check")

#: Ceiling for one ffprobe/ffmpeg call. The decode is bounded work on a
#: window of at most `sample_seconds`, so anything near this is a hang.
_FFMPEG_TIMEOUT_S = 120

try:
    import numpy as np
    import soundfile as sf

    _AUDIO_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - both are install dependencies
    np = None  # type: ignore[assignment]
    sf = None  # type: ignore[assignment]
    _AUDIO_IMPORT_ERROR = exc


CONFIDENCE_CERTAIN = "certain"
CONFIDENCE_LIKELY = "likely"
CONFIDENCE_SUSPECT = "suspect"

ARTIFACT_SAMPLE_HOLD = "sample_hold"
ARTIFACT_INTERPOLATION = "linear_interpolation"
ARTIFACT_IMAGING = "imaging"

FLOOR_BELOW_16BIT = "below_16bit"
FLOOR_AT_16BIT = "at_16bit"
FLOOR_MASKED = "masked"

#: Standard-definition rates a fake is made from; their Nyquist is where a
#: resampler's anti-imaging filter leaves its cliff.
_SOURCE_RATES = (44100, 48000)

#: Band the in-band noise floor is measured over. Noise-shaped dither lowers
#: the floor in the mid-band only by raising it above ~15 kHz, so averaging
#: over the whole band keeps a shaped 16-bit floor at or above the flat one.
_FLOOR_BAND_LOW_HZ = 500.0
_FLOOR_BAND_HIGH_HZ = 20000.0
#: Fraction of fully-inside STFT frames, quietest first, the floor is read in.
_QUIET_FRAME_FRACTION = 0.1
#: Floor against flat 16-bit quantization noise: below this there is
#: resolution a 16-bit copy would lose; above _FLOOR_MASKED_DB the quietest
#: frames still carry music and the floor cannot be read.
_FLOOR_BELOW_16BIT_DB = -6.0
_FLOOR_MASKED_DB = 20.0
#: Brickwall: flat passband before the source Nyquist, a cliff after it.
_BRICKWALL_MAX_PASSBAND_DROP_DB = 20.0
_BRICKWALL_MIN_CLIFF_DB = 40.0
_BRICKWALL_FLAT_PASSBAND_DB = 6.0
#: Imaging: the band above the source Nyquist mirrors the one below.
_IMAGING_MIN_CORRELATION = 0.9
#: Integer-upsampling artifacts: the signal must move at enough original
#: samples, and (almost) never between them.
_ARTIFACT_MIN_MOVING_ANCHORS = 1000
_ARTIFACT_MAX_VIOLATION_RATE = 0.001
#: Frames per rfft batch: bounds memory at ~8 MB per batch for n_fft=4096.
_STFT_BATCH_FRAMES = 256
#: Music bandwidth: 1 kHz bands from _MUSIC_BAND_START_HZ up are music while
#: their level swings with it across frames (p95-p5 at least
#: _MUSIC_MIN_SPREAD_DB). Steady noise, such as the ultrasonic hump a DSD or
#: tape transfer carries up to Nyquist, averages out to a few dB.
_MUSIC_BAND_START_HZ = 16000.0
_MUSIC_BAND_WIDTH_HZ = 1000.0
_MUSIC_MIN_SPREAD_DB = 10.0
#: Active content this far past the music bandwidth is reported as steady
#: noise rather than content.
_ULTRASONIC_NOISE_MARGIN_HZ = 8000.0
#: Standard rate families; a file's useful rate is looked up in its own.
_RATE_FAMILIES = (
    (44100, 88200, 176400, 352800),
    (48000, 96000, 192000, 384000),
)


class HiResCheckError(Exception):
    """Raised when the spectral analysis cannot be completed.

    Always safe to catch broadly and treat as "verification skipped" —
    it is never raised for reasons that should abort a download.
    """


@dataclass(frozen=True)
class HiResCheckResult:
    """Outcome of a single-file spectral analysis."""

    file_path: str
    declared_sample_rate: int
    total_duration_s: float
    analyzed_duration_s: float
    cutoff_frequency_hz: float
    noise_floor_db: float
    verdict: (
        str  # "fake_hires" | "standard_definition" | "genuine_hires" | "inconclusive"
    )
    #: Bits per sample the container declares, or 0 when the format has no
    #: fixed-point depth to declare (a lossy codec, or float PCM).
    declared_bit_depth: int = 0
    #: Bits per sample that actually carry data. 0 when not measured.
    effective_bit_depth: int = 0
    #: Why this file was flagged, in one clause; empty when it was not.
    #: Built where the verdict is, because only there is it known which of
    #: the two claims the file actually made — a 24-bit/44.1 kHz file has a
    #: CD-range cutoff by definition, and reading that back off the numbers
    #: alone would report a spectral finding nobody made.
    reason: str = ""
    #: How sure a "fake_hires" verdict is: "certain", "likely" or "suspect"
    #: (see the module docstring). Empty for every other verdict.
    confidence: str = ""
    #: Exact upsampling fingerprint found, if any: "sample_hold",
    #: "linear_interpolation" or "imaging".
    upsampling_artifact: str = ""
    #: Source Nyquist (22050 / 24000) with a resampler-style cliff; 0 if none.
    brickwall_hz: float = 0.0
    #: In-band floor of the quietest frames against flat 16-bit quantization
    #: noise: "below_16bit", "at_16bit", "masked", or empty when not measured.
    noise_floor_class: str = ""
    noise_floor_vs_16bit_db: float = 0.0
    #: Highest frequency whose level still moves with the music, for a file
    #: claiming Hi-Res by rate; 0 when not measured. Informational: the
    #: verdict rests on the tests above.
    music_cutoff_hz: float = 0.0
    #: True when the active content past music_cutoff_hz is steady noise,
    #: like the ultrasonic hump of a DSD or analog tape transfer, so
    #: cutoff_frequency_hz marks where that noise ends rather than the music.
    ultrasonic_noise_only: bool = False
    #: With ultrasonic_noise_only: the lowest standard rate of the same
    #: family that holds all the music (e.g. 88200 for a 176.4 kHz file whose
    #: music stops at 34 kHz). Otherwise the declared rate; 0 if not set.
    useful_sample_rate: int = 0

    @property
    def is_suspicious(self) -> bool:
        """True for any "fake hi-res" verdict, whatever its confidence."""
        return self.verdict == "fake_hires"

    @property
    def redownload_safe(self) -> bool:
        """True when a LOSSLESS copy would lose nothing this file holds.

        A certain or likely fake. A suspect may be a genuine master that a
        16-bit copy would strip of real bit depth, so it is only reported.
        """
        return self.is_suspicious and self.confidence in (
            CONFIDENCE_CERTAIN,
            CONFIDENCE_LIKELY,
        )

    @property
    def padded_bit_depth(self) -> bool:
        """True when the declared depth is real bits the file never uses."""
        return self.declared_bit_depth > 16 and 0 < self.effective_bit_depth <= 16

    def summary(self) -> str:
        fake_labels = {
            CONFIDENCE_CERTAIN: "FAKE HI-RES",
            CONFIDENCE_LIKELY: "LIKELY FAKE HI-RES",
            CONFIDENCE_SUSPECT: "POSSIBLY FAKE HI-RES",
        }
        fake_label = fake_labels.get(self.confidence, "LIKELY FAKE HI-RES")
        labels = {
            "fake_hires": (
                f"{fake_label} — {self.reason}" if self.reason else fake_label
            ),
            "standard_definition": (
                "Standard-definition file — nothing to flag "
                "(neither the sample rate nor the bit depth claims Hi-Res)"
            ),
            # Deliberately not "content extends past CD limits": a genuine
            # 24-bit/44.1 kHz file claims Hi-Res by depth alone, and its
            # content stops at 22.05 kHz by definition. Saying otherwise
            # would report a measurement the file cannot possibly pass.
            "genuine_hires": (
                "Measures as genuine Hi-Res — no upsampling or padded bit depth found"
            ),
            "inconclusive": (
                "Inconclusive — the analyzed segment was too quiet, short, "
                "or silent to draw a reliable conclusion"
            ),
        }
        return (
            f"{self.file_path}\n"
            f"  Declared sample rate : {self.declared_sample_rate} Hz\n"
            f"  Analyzed segment     : {self.analyzed_duration_s:.1f}s "
            f"(of {self.total_duration_s:.1f}s total)\n"
            f"  Active cutoff freq.  : ~{self.cutoff_frequency_hz:.0f} Hz "
            f"(noise floor: {self.noise_floor_db:.0f} dB)\n"
            f"  Bit depth            : {self.declared_bit_depth or '?'} declared, "
            f"{self.effective_bit_depth or '?'} in use\n"
            f"  Verdict              : {labels.get(self.verdict, self.verdict)}"
        )


#: soundfile subtypes that carry a fixed-point sample depth. Float PCM and
#: the lossy codecs are deliberately absent: neither has a bit depth that
#: could be padded, so there is nothing for this test to say about them.
_PCM_SUBTYPE_BITS = {
    "PCM_S8": 8,
    "PCM_U8": 8,
    "PCM_16": 16,
    "PCM_24": 24,
    "PCM_32": 32,
}


@dataclass(frozen=True)
class _IntWindow:
    """The integer-sample view of one window: depth, plus channel 0."""

    declared_bits: int = 0
    effective_bits: int = 0
    #: Channel 0, right-justified at the declared depth; None when unread.
    first_channel: Any = None
    #: Low bits of the declared depth no sample in the window ever uses.
    unused_bits: int = 0


def _read_int_window(path: Path, start_frame: int, frames: int) -> _IntWindow:
    """Declared/effective depth over one window, plus channel 0's samples.

    Effective depth is counted, not estimated: soundfile hands back every
    PCM subtype left-justified in an int32, so OR-ing the whole window
    together and counting the low bits that stayed zero says exactly how
    many bits the file ever puts to use. A 16-bit master padded into a
    24-bit container leaves its bottom 8 bits zero in every sample and is
    caught with certainty — there is no threshold here to tune or to argue
    with.

    Returns an empty window for anything with no fixed-point depth to check,
    and for any read failure: an unmeasurable depth must leave the spectral
    verdict exactly as it was, never turn into a finding of its own.
    """
    via_ffmpeg = False
    try:
        declared = _PCM_SUBTYPE_BITS.get(sf.info(path).subtype, 0)
    except Exception as exc:
        if not _ffmpeg_available():
            logger.debug("[hires-check] could not read subtype of '%s': %s", path, exc)
            return _IntWindow()
        try:
            _sr, _frames, declared = _ffprobe_info(path)
        except Exception as probe_exc:
            logger.debug(
                "[hires-check] ffprobe could not read '%s': %s", path, probe_exc
            )
            return _IntWindow()
        via_ffmpeg = True
    if not declared:
        return _IntWindow()

    try:
        if via_ffmpeg:
            sample_rate, _f, _b = _ffprobe_info(path)
            window = _ffmpeg_read_window(path, start_frame, frames, sample_rate)
        else:
            with sf.SoundFile(path) as handle:
                handle.seek(start_frame)
                window = handle.read(frames, dtype="int32", always_2d=True)
    except Exception as exc:
        logger.debug("[hires-check] could not read samples of '%s': %s", path, exc)
        return _IntWindow(declared_bits=declared)

    if window.size == 0:
        return _IntWindow(declared_bits=declared)

    accumulated = int(np.bitwise_or.reduce(window.astype(np.uint32).ravel()))
    if accumulated == 0:
        # Digital silence carries no bits at all. Reporting 0 keeps it out
        # of the padded-depth test rather than making every silent passage
        # look like a 16-bit fake.
        return _IntWindow(declared_bits=declared)

    unused_low_bits = int((accumulated & -accumulated).bit_length() - 1)
    justify = 32 - declared
    return _IntWindow(
        declared_bits=declared,
        effective_bits=32 - unused_low_bits,
        first_channel=window[:, 0].astype(np.int64) >> justify,
        unused_bits=max(unused_low_bits - justify, 0),
    )


def is_available() -> bool:
    """Whether the analysis can run at all.

    Both numpy and soundfile are install dependencies now, so this is True
    on any working install. Kept — and still checked by every caller —
    because a broken environment is a thing that happens, and degrading to
    "verification skipped" is the right answer when it does. It must never
    be the reason a finished download is treated as failed.
    """
    return _AUDIO_IMPORT_ERROR is None


def _require_audio_libs() -> None:
    if _AUDIO_IMPORT_ERROR is not None:
        raise HiResCheckError(
            "Hi-Res verification needs 'numpy' and 'soundfile', which are "
            "install dependencies of SpotiFLAC — this environment has a "
            "broken or partial install. Try: pip install --force-reinstall "
            f"numpy soundfile. Original import error: {_AUDIO_IMPORT_ERROR}"
        )


# ── ffmpeg fallback ────────────────────────────────────────────────────────
#
# libsndfile, which soundfile wraps, cannot open an MP4 container (so no
# ALAC), WavPack or TTA. Three of `--transcode`'s seven targets land in
# exactly those, and only the *lossy* ones are deliberately skipped by the
# download path — so `--transcode alac --verify-hires` used to look enabled
# while checking nothing at all, one debug line at a time.
#
# ffmpeg reads all of them, and anyone holding files in those formats
# because SpotiFLAC converted them has it installed by definition:
# ensure_ffmpeg_available() runs before the first download. When it is
# missing the callers still degrade to "verification skipped" — but they say
# so out loud now.


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _ffprobe_info(path: Path) -> tuple[int, int, int]:
    """(sample_rate, frames, declared_bits) via ffprobe.

    `bits_per_raw_sample` is what the codec actually stores; ffprobe leaves
    it empty for formats that have no fixed depth, which is the same "0
    means no claim to check" convention _read_int_window() already uses.
    """
    out = subprocess.run(  # noqa: S603 - fixed argv, path passed as one arg
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,bits_per_raw_sample,duration_ts,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=_FFMPEG_TIMEOUT_S,
        check=True,
    )
    streams = json.loads(out.stdout or "{}").get("streams") or []
    if not streams:
        raise HiResCheckError(f"ffprobe found no audio stream in '{path}'")
    stream = streams[0]

    sample_rate = int(stream.get("sample_rate") or 0)
    if sample_rate <= 0:
        raise HiResCheckError(f"ffprobe reported no sample rate for '{path}'")

    # duration_ts is in stream time base, which for audio is the sample
    # rate, so it is the frame count directly. Falling back to seconds
    # keeps containers that only carry a duration usable.
    frames = int(stream.get("duration_ts") or 0)
    if frames <= 0:
        frames = int(float(stream.get("duration") or 0.0) * sample_rate)

    try:
        declared_bits = int(stream.get("bits_per_raw_sample") or 0)
    except (TypeError, ValueError):
        declared_bits = 0

    return sample_rate, frames, declared_bits


def _ffmpeg_read_window(
    path: Path,
    start_frame: int,
    frames: int,
    sample_rate: int,
) -> "np.ndarray":
    """The requested window decoded to interleaved 32-bit PCM, 2D (n, ch).

    s32le on purpose, and left-justified the way soundfile hands back every
    PCM subtype: ffmpeg widens a 16-bit source by shifting it up, so the low
    bits stay zero and the padded-depth test reads a converted file exactly
    as it reads a native one.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, path passed as one arg
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{start_frame / sample_rate:.6f}",
            "-t",
            f"{frames / sample_rate:.6f}",
            "-i",
            str(path),
            "-map",
            "a:0",
            "-f",
            "s32le",
            "-acodec",
            "pcm_s32le",
            "-",
        ],
        capture_output=True,
        timeout=_FFMPEG_TIMEOUT_S,
        check=True,
    )
    raw = np.frombuffer(proc.stdout, dtype="<i4")
    if raw.size == 0:
        raise HiResCheckError(f"ffmpeg decoded an empty window from '{path}'")

    channels = max(1, _ffprobe_channels(path))
    usable = (raw.size // channels) * channels
    return raw[:usable].reshape(-1, channels)


def _ffprobe_channels(path: Path) -> int:
    out = subprocess.run(  # noqa: S603 - fixed argv, path passed as one arg
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=_FFMPEG_TIMEOUT_S,
        check=True,
    )
    try:
        return int((out.stdout or "").strip() or 0)
    except ValueError:
        return 0


def _read_mono_window(
    path: Path,
    start_frame: int,
    frames: int,
) -> tuple["np.ndarray", int, int]:
    """(mono float32 in full-scale units, sample rate, channel count).

    soundfile first, and ffmpeg for what libsndfile cannot open (ALAC/MP4,
    WavPack, TTA). Both come back in full-scale units (±1.0): the noise
    floor test reads absolute levels against 16-bit quantization noise, so
    the ffmpeg path's left-justified int32 is scaled down to match.
    """
    try:
        with sf.SoundFile(path) as handle:
            handle.seek(start_frame)
            block = handle.read(frames, dtype="float32", always_2d=True)
            return block.mean(axis=1), int(handle.samplerate), int(block.shape[1])
    except Exception as exc:
        if not _ffmpeg_available():
            raise HiResCheckError(
                f"{path.suffix or 'this format'} cannot be opened by "
                f"libsndfile ({exc}), and ffmpeg — which reads it — is not "
                "installed"
            ) from exc
        logger.debug(
            "[hires-check] soundfile could not open '%s' (%s); using ffmpeg",
            path,
            exc,
        )

    sample_rate, _frames, _bits = _ffprobe_info(path)
    window = _ffmpeg_read_window(path, start_frame, frames, sample_rate)
    mono = (window.astype(np.float64) / 2.0**31).mean(axis=1)
    return mono.astype(np.float32), sample_rate, int(window.shape[1])


# ── Spectrum and evidence ──────────────────────────────────────────────────


def _analyze_stft(
    y: "np.ndarray", n_fft: int, sample_rate: int
) -> tuple["np.ndarray", float, list[float]]:
    """(mean magnitude spectrum, quiet-frame floor, music band spreads).

    The spectrum matches `np.abs(librosa.stft(y, n_fft)).mean(axis=1)`: a
    periodic Hann window, a hop of n_fft/4, and centred frames (the signal
    zero-padded by n_fft/2 at both ends). Checked against librosa before it
    was dropped: the spectra agreed to float rounding and every cutoff came
    out identical.

    The floor is the white-noise-equivalent variance, in full-scale units,
    of the quietest tenth of the frames that lie fully inside the signal,
    or NaN when none qualified. Frames overlapping the zero padding would
    read as quiet for the wrong reason, and digital silence has no floor.

    The spreads are the p95-p5 swing, across those same frames, of each
    1 kHz band's level from 16 kHz up (see _music_cutoff); empty for files
    at or below 48 kHz, where the question does not arise.
    """
    hop = n_fft // 4
    half = n_fft // 2
    avg = np.zeros(half + 1, dtype=np.float64)
    # Annotated because numpy is `follow_imports = "skip"` for mypy (see
    # pyproject.toml), so everything it returns arrives as untyped Any.
    signal: Any = np.asarray(y, dtype=np.float64)
    padded: Any = np.pad(signal, half, mode="constant")
    frame_count = 1 + (len(padded) - n_fft) // hop
    if frame_count < 1:
        return avg, math.nan, []

    window: Any = np.hanning(n_fft + 1)[:-1]
    window_power = float(np.sum(window * window))
    bin_hz = sample_rate / n_fft
    band_low = math.ceil(_FLOOR_BAND_LOW_HZ / bin_hz)
    band_high = min(int(_FLOOR_BAND_HIGH_HZ / bin_hz), half)
    offsets: Any = np.arange(n_fft)

    music_bands: list[tuple[int, int]] = []
    if sample_rate / 2 > _SOURCE_RATES[1] / 2:
        lo = _MUSIC_BAND_START_HZ
        while lo + _MUSIC_BAND_WIDTH_HZ <= sample_rate / 2:
            first_bin = math.ceil(lo / bin_hz)
            last_bin = min(math.ceil((lo + _MUSIC_BAND_WIDTH_HZ) / bin_hz), half + 1)
            if last_bin > first_bin:
                music_bands.append((first_bin, last_bin))
            lo += _MUSIC_BAND_WIDTH_HZ

    frame_means: list[Any] = []
    frame_medians: list[Any] = []
    music_levels: list[Any] = []
    # Batched rather than one strided view over every frame: a 30s window
    # at 176.4 kHz is ~5000 frames, and their spectra all at once run to
    # hundreds of megabytes.
    for first in range(0, frame_count, _STFT_BATCH_FRAMES):
        index: Any = np.arange(first, min(first + _STFT_BATCH_FRAMES, frame_count))
        starts: Any = hop * index
        frames = padded[starts[:, None] + offsets[None, :]] * window
        magnitude: Any = np.abs(np.fft.rfft(frames, n=n_fft, axis=1))
        avg += magnitude.sum(axis=0)

        if band_high <= band_low:
            continue
        inside = (starts - half >= 0) & (starts - half + n_fft <= len(signal))
        power = magnitude[inside, band_low : band_high + 1] ** 2
        keep = power.sum(axis=1) > 0
        power = power[keep]
        if power.size:
            frame_means.append(power.mean(axis=1))
            frame_medians.append(np.median(power, axis=1))
            if music_bands:
                full: Any = magnitude[inside][keep] ** 2
                music_levels.append(
                    np.stack(
                        [full[:, a:b].mean(axis=1) for a, b in music_bands], axis=1
                    )
                )

    avg /= frame_count
    if not frame_means:
        return avg, math.nan, []

    spreads: list[float] = []
    if music_levels:
        levels: Any = 10 * np.log10(np.maximum(np.concatenate(music_levels), 1e-30))
        if levels.shape[0] >= 2:
            spreads = [
                float(np.percentile(col, 95) - np.percentile(col, 5))
                for col in levels.T
            ]

    means: Any = np.concatenate(frame_means)
    medians: Any = np.concatenate(frame_medians)
    count = max(1, int(len(means) * _QUIET_FRAME_FRACTION))
    quietest = medians[np.argsort(means, kind="stable")[:count]]
    # |X|^2 of white noise is exponential with mean sigma^2 * sum(w^2), so
    # its median is ln 2 times that. The median ignores tonal peaks.
    floor_var = float(np.median(quietest / math.log(2) / window_power))
    return avg, floor_var, spreads


def _music_cutoff(
    spreads: list[float],
    spec_db: "np.ndarray",
    sample_rate: int,
    n_fft: int,
    noise_floor_db: float,
) -> float:
    """Upper edge of the last contiguous 1 kHz band, from 16 kHz up, that
    both carries active content (its level in the averaged spectrum above
    noise_floor_db) and moves with the music.

    The level test keeps a resampler's leakage, which swings with the music
    too but sits far below it, from counting. 0 when even the first fails.
    """
    cutoff = 0.0
    for index, spread in enumerate(spreads):
        lo = _MUSIC_BAND_START_HZ + index * _MUSIC_BAND_WIDTH_HZ
        band = _band(spec_db, sample_rate, n_fft, lo, lo + _MUSIC_BAND_WIDTH_HZ)
        if not band.size or spread < _MUSIC_MIN_SPREAD_DB:
            break
        if float(np.mean(band)) <= noise_floor_db:
            break
        cutoff = lo + _MUSIC_BAND_WIDTH_HZ
    return cutoff


def _useful_sample_rate(declared: int, music_cutoff_hz: float) -> int:
    """Lowest standard rate in the declared rate's family whose Nyquist still
    holds music_cutoff_hz; the declared rate when none below it does."""
    for family in _RATE_FAMILIES:
        if declared % family[0]:
            continue
        for rate in family:
            if rate >= declared:
                break
            if rate / 2 >= music_cutoff_hz:
                return rate
    return declared


def _classify_noise_floor(floor_var: float, channels: int) -> tuple[str, float]:
    """The floor against flat 16-bit quantization noise.

    LSB^2/12 per channel, divided by the channel count for the mono downmix
    of independent channels: the lowest a 16-bit source's floor can be.
    """
    if math.isnan(floor_var) or floor_var <= 0:
        return "", 0.0
    lsb = 2.0**-15
    ref = lsb * lsb / 12 / max(channels, 1)
    vs_16bit_db = 10 * math.log10(floor_var / ref)
    if vs_16bit_db < _FLOOR_BELOW_16BIT_DB:
        return FLOOR_BELOW_16BIT, vs_16bit_db
    if vs_16bit_db > _FLOOR_MASKED_DB:
        return FLOOR_MASKED, vs_16bit_db
    return FLOOR_AT_16BIT, vs_16bit_db


def _band(
    spec_db: "np.ndarray", sample_rate: int, n_fft: int, low_hz: float, high_hz: float
) -> "np.ndarray":
    bin_hz = sample_rate / n_fft
    lo = max(math.ceil(low_hz / bin_hz), 0)
    hi = min(int(high_hz / bin_hz), len(spec_db) - 1)
    return spec_db[lo : hi + 1]


def _detect_brickwall(
    spec_db: "np.ndarray", sample_rate: int, n_fft: int, noise_floor_db: float
) -> float:
    """The source Nyquist (22050 / 24000 Hz) with a resampler's cliff, or 0.

    A resampler's anti-imaging filter leaves the spectrum flat right up to
    the edge and then drops it off a cliff. A mastering low-pass rolls off
    gradually and is already well down before the edge.

    A 48 kHz source passes the test at 22.05 kHz too (its cliff lies beyond
    that edge as well), so the highest edge the passband still reaches flat
    wins; a 44.1 kHz source is already sloping into its transition band there.
    """
    best, best_drop = 0.0, math.inf
    for rate in _SOURCE_RATES:
        edge = rate / 2
        if edge + 5000 > sample_rate / 2:
            continue
        passband = _band(spec_db, sample_rate, n_fft, edge - 8000, edge - 5000)
        below = _band(spec_db, sample_rate, n_fft, edge - 2500, edge - 500)
        above = _band(spec_db, sample_rate, n_fft, edge + 2500, edge + 5000)
        if not (passband.size and below.size and above.size):
            continue
        below_level = float(np.median(below))
        drop = float(np.median(passband)) - below_level
        cliff = below_level - float(np.median(above))
        if (
            below_level <= noise_floor_db
            or drop > _BRICKWALL_MAX_PASSBAND_DROP_DB
            or cliff < _BRICKWALL_MIN_CLIFF_DB
        ):
            continue
        flat = drop <= _BRICKWALL_FLAT_PASSBAND_DB
        best_flat = best_drop <= _BRICKWALL_FLAT_PASSBAND_DB
        if (
            best == 0
            or (flat and (not best_flat or edge > best))
            or (not flat and not best_flat and drop < best_drop)
        ):
            best, best_drop = edge, drop
    return best


def _detect_imaging(
    spec_db: "np.ndarray", sample_rate: int, n_fft: int, noise_floor_db: float
) -> bool:
    """Whether the band above a source Nyquist mirrors the band below it.

    That is what upsampling without (or with a poor) anti-imaging filter
    leaves. Genuine content keeps falling with frequency, so its mirror
    correlation is near zero or negative.
    """
    bin_hz = sample_rate / n_fft
    for rate in _SOURCE_RATES:
        low_hz = rate / 2 + 500
        high_hz = min(rate - 500, sample_rate / 2 - 500)
        if high_hz - low_hz < 2000:
            continue
        image: list[float] = []
        mirror: list[float] = []
        k = math.ceil(low_hz / bin_hz)
        while k * bin_hz <= high_hz:
            # Rounded half away from zero, as Go's math.Round does.
            m = int(math.floor((rate - k * bin_hz) / bin_hz + 0.5))
            if k < len(spec_db) and 0 <= m < len(spec_db):
                image.append(float(spec_db[k]))
                mirror.append(float(spec_db[m]))
            k += 1
        if len(image) < 16 or sum(image) / len(image) <= noise_floor_db:
            continue  # no content above the edge: nothing was mirrored
        if _pearson(image, mirror) >= _IMAGING_MIN_CORRELATION:
            return True
    return False


def _detect_integer_upsampling(window: _IntWindow, sample_rate: int) -> str:
    """Exact fingerprints of upsampling by an integer ratio from 44.1/48 kHz.

    Every sample repeated (sample-and-hold), or the in-between samples on a
    straight line (linear interpolation). Measured in units of the bits
    actually used, so a padded source's rounding stays within a couple of
    its own LSBs.
    """
    samples = window.first_channel
    if samples is None or len(samples) < 4:
        return ""
    x: Any = samples >> window.unused_bits
    d1: Any = x[1:-1] - x[:-2]
    d2: Any = np.abs(x[:-2] - 2 * x[1:-1] + x[2:])
    positions: Any = np.arange(1, len(x) - 1)
    for rate in _SOURCE_RATES:
        if sample_rate % rate:
            continue
        ratio = sample_rate // rate
        if not 2 <= ratio <= 8:
            continue
        for phase in range(ratio):
            inner = (positions - phase) % ratio != 0  # between original samples
            max_violations = int(inner.sum()) * _ARTIFACT_MAX_VIOLATION_RATE
            if (
                int((d1[~inner] != 0).sum()) >= _ARTIFACT_MIN_MOVING_ANCHORS
                and int((d1[inner] != 0).sum()) <= max_violations
            ):
                return ARTIFACT_SAMPLE_HOLD
            if (
                int((d2[~inner] > 2).sum()) >= _ARTIFACT_MIN_MOVING_ANCHORS
                and int((d2[inner] > 2).sum()) <= max_violations
            ):
                return ARTIFACT_INTERPOLATION
    return ""


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = var_a = var_b = 0.0
    for va, vb in zip(a, b):
        da, db = va - mean_a, vb - mean_b
        cov += da * db
        var_a += da * da
        var_b += db * db
    if var_a == 0 or var_b == 0:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def check_file(
    file_path: str | Path,
    sample_seconds: int = 30,
    noise_floor_db: float = -80.0,
    hires_sample_rate_threshold: int = 48000,
    hires_cutoff_threshold_hz: float = 28000.0,
    n_fft: int = 4096,
) -> HiResCheckResult:
    """Analyzes ``file_path`` and returns a :class:`HiResCheckResult`.

    Loads only a short segment from the middle of the track (never the
    whole file) to keep memory usage bounded regardless of track length.

    Args:
        file_path: Path to any audio file. libsndfile handles FLAC, WAV,
            AIFF, OGG, MP3 and the rest of its list directly; ALAC/MP4,
            WavPack and TTA go through ffmpeg instead, which is why three
            of `--transcode`'s targets are checkable at all. Without ffmpeg
            those three raise HiResCheckError, and the message says which
            half is missing rather than reporting a corrupt file.
        sample_seconds: Length, in seconds, of the segment to analyze.
            Clamped to the file's actual duration if shorter.
        noise_floor_db: dB threshold (relative to the segment's peak)
            above which a frequency bin is considered "active" content
            rather than noise/silence.
        hires_sample_rate_threshold: Sample rate (Hz) above which a file
            is considered to *claim* Hi-Res.
        hires_cutoff_threshold_hz: Minimum active-content cutoff frequency
            (Hz) a genuine Hi-Res file is expected to reach. Sits well
            above 22.05 kHz on purpose: a resampler upsampling from CD
            leaves a transition-band tail a couple of kHz wide, which a
            threshold hugging the CD Nyquist reads as real content. A
            measured 44.1 -> 176.4 kHz upsample of a commercial track
            reached ~24.7 kHz — the old 24 kHz default passed it.
        n_fft: FFT window size for the STFT. Automatically shrunk for very
            short segments, so a window longer than the audio cannot end up
            measuring its own zero padding.

    Returns:
        A populated HiResCheckResult. Never returns partial/garbage data —
        any failure raises HiResCheckError instead.

    Raises:
        HiResCheckError: for any condition that prevents a reliable
            analysis (missing dependency, missing/empty/corrupt file,
            unreadable audio, invalid parameters, fully silent segment
            after decoding that also fails the safety net below).
    """
    _require_audio_libs()

    if sample_seconds <= 0:
        raise HiResCheckError("sample_seconds must be a positive number")
    if n_fft <= 0 or (n_fft & (n_fft - 1)) != 0:
        raise HiResCheckError("n_fft must be a positive power of two")

    path = Path(file_path)
    try:
        exists = path.is_file()
    except OSError as exc:
        raise HiResCheckError(f"Cannot access path '{path}': {exc}") from exc
    if not exists:
        raise HiResCheckError(f"File not found: {path}")

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HiResCheckError(f"Cannot stat file '{path}': {exc}") from exc
    if size == 0:
        raise HiResCheckError(f"File is empty: {path}")

    # One header read for both figures, and no decoding to get them: the
    # frame count is in the header, so duration is arithmetic rather than a
    # full pass over the file.
    try:
        info = sf.info(path)
        declared_sr = int(info.samplerate)
        total_frames = int(info.frames)
    except Exception as exc:
        if not _ffmpeg_available():
            raise HiResCheckError(
                f"Could not read the audio header of '{path.name}': {exc}. "
                "libsndfile cannot open MP4/ALAC, WavPack or TTA; ffmpeg "
                "reads all three but is not installed."
            ) from exc
        try:
            declared_sr, total_frames, _bits = _ffprobe_info(path)
        except Exception as probe_exc:
            raise HiResCheckError(
                f"Neither libsndfile nor ffmpeg could read '{path.name}': "
                f"{exc} / {probe_exc}"
            ) from probe_exc
    if declared_sr <= 0:
        raise HiResCheckError(f"Invalid declared sample rate: {declared_sr}")

    total_duration = total_frames / declared_sr
    if total_duration <= 0:
        raise HiResCheckError(
            "File reports zero or negative duration — likely corrupt/unreadable"
        )

    analyzed_duration = min(float(sample_seconds), total_duration)
    offset = max(0.0, (total_duration - analyzed_duration) / 2)

    start_frame = int(offset * declared_sr)
    window_frames = int(analyzed_duration * declared_sr)
    try:
        y, sr, channels = _read_mono_window(path, start_frame, window_frames)
    except Exception as exc:
        raise HiResCheckError(f"Could not decode audio: {exc}") from exc

    if y is None or getattr(y, "size", 0) == 0:
        raise HiResCheckError("Decoded audio segment is empty")
    if sr <= 0:
        raise HiResCheckError(f"Decoder returned an invalid sample rate: {sr}")

    def inconclusive() -> HiResCheckResult:
        return HiResCheckResult(
            file_path=str(path),
            declared_sample_rate=int(sr),
            total_duration_s=total_duration,
            analyzed_duration_s=analyzed_duration,
            cutoff_frequency_hz=0.0,
            noise_floor_db=noise_floor_db,
            verdict="inconclusive",
        )

    # A fully-silent (or near-silent) segment makes spectral analysis
    # meaningless rather than wrong — report it as inconclusive instead of
    # guessing.
    if not np.any(np.abs(y) > 1e-9):
        return inconclusive()

    # Shrink n_fft for very short segments so a huge window is not padded
    # over a tiny signal, which would measure the padding as much as the
    # audio.
    effective_n_fft = n_fft
    while effective_n_fft > 256 and effective_n_fft > len(y) * 2:
        effective_n_fft //= 2

    try:
        avg_spectrum, quiet_floor_var, music_spreads = _analyze_stft(
            y, effective_n_fft, sr
        )
        if avg_spectrum.size == 0:
            raise HiResCheckError("Spectral analysis produced no bins")
        peak = float(np.max(avg_spectrum))
        if peak <= 0.0:
            return inconclusive()
        # Deliberately unclamped. librosa's amplitude_to_db, which this
        # replaces, floors everything at `peak - 80 dB` by default —
        # exactly where noise_floor_db also sits — so every floored bin
        # compared as "active" against any lower threshold, and the check
        # reported the full Nyquist frequency as the cutoff for every file
        # it was given. Documented as tunable, the parameter silently
        # disabled the check at any value under its own default.
        spectrum_db = 20.0 * np.log10(np.maximum(avg_spectrum, 1e-30) / peak)
        frequencies = np.fft.rfftfreq(effective_n_fft, 1.0 / sr)
    except HiResCheckError:
        raise
    except Exception as exc:
        raise HiResCheckError(f"Spectral analysis failed: {exc}") from exc

    active = frequencies[spectrum_db > noise_floor_db]
    cutoff = float(active[-1]) if active.size else 0.0

    int_window = _read_int_window(path, start_frame=start_frame, frames=window_frames)
    declared_bits = int_window.declared_bits
    effective_bits = int_window.effective_bits

    # A file can claim Hi-Res by rate, by depth, or by both, and each claim
    # is answered by the test that can actually judge it. Keeping them
    # separate is what lets a 24-bit/44.1 kHz file be judged at all: it
    # claims nothing by rate, so the spectral test has no opinion on it,
    # and treating "no opinion" as "nothing to flag" is how a padded CD
    # master used to pass without being looked at.
    claims_by_rate = sr > hires_sample_rate_threshold
    claims_by_depth = declared_bits > 16
    music_cutoff_hz = 0.0
    ultrasonic_noise_only = False
    useful_sample_rate = int(sr)
    if claims_by_rate:
        music_cutoff_hz = _music_cutoff(
            music_spreads, spectrum_db, sr, effective_n_fft, noise_floor_db
        )
        if (
            music_cutoff_hz > 0
            and cutoff - music_cutoff_hz >= _ULTRASONIC_NOISE_MARGIN_HZ
        ):
            ultrasonic_noise_only = True
            useful_sample_rate = _useful_sample_rate(int(sr), music_cutoff_hz)
    # A resampler's cliff at 22.05/24 kHz betrays a 44.1/48 kHz chain even
    # when a weak stopband leaves a flat plateau above it that reads as
    # "content" to the cutoff test (ffmpeg's default resampler does this).
    brickwall_hz = (
        _detect_brickwall(spectrum_db, sr, effective_n_fft, noise_floor_db)
        if claims_by_rate
        else 0.0
    )
    cutoff_is_low = claims_by_rate and cutoff < hires_cutoff_threshold_hz
    rate_is_fake = cutoff_is_low or brickwall_hz > 0
    depth_is_fake = claims_by_depth and 0 < effective_bits <= 16

    # Exact fingerprints of a conversion. Imaging puts content back above
    # 22 kHz, so such a file can pass the cutoff test and still be a fake.
    artifact = ""
    if claims_by_rate:
        artifact = _detect_integer_upsampling(int_window, sr)
        if not artifact and _detect_imaging(
            spectrum_db, sr, effective_n_fft, noise_floor_db
        ):
            artifact = ARTIFACT_IMAGING

    floor_class, floor_vs_16bit_db = (
        _classify_noise_floor(quiet_floor_var, channels) if rate_is_fake else ("", 0.0)
    )

    if not (claims_by_rate or claims_by_depth):
        verdict = "standard_definition"
    elif rate_is_fake or depth_is_fake or artifact:
        verdict = "fake_hires"
    else:
        verdict = "genuine_hires"

    confidence = ""
    if verdict == "fake_hires":
        if depth_is_fake or artifact:
            confidence = CONFIDENCE_CERTAIN
        elif brickwall_hz > 0 and floor_class == FLOOR_AT_16BIT:
            confidence = CONFIDENCE_LIKELY
        else:
            confidence = CONFIDENCE_SUSPECT

    findings = []
    if artifact == ARTIFACT_SAMPLE_HOLD:
        findings.append("every sample is repeated (sample-and-hold upsampling)")
    elif artifact == ARTIFACT_INTERPOLATION:
        findings.append("in-between samples are linearly interpolated")
    elif artifact == ARTIFACT_IMAGING:
        findings.append("content above the source Nyquist mirrors the audible band")
    if cutoff_is_low:
        findings.append(f"declares {int(sr)} Hz but content stops at ~{cutoff:.0f} Hz")
    elif rate_is_fake:
        findings.append(
            f"declares {int(sr)} Hz but the spectrum falls off a cliff "
            f"at {brickwall_hz:.0f} Hz"
        )
    if depth_is_fake:
        findings.append(
            f"declares {declared_bits}-bit but only {effective_bits} bits carry data"
        )
    if confidence == CONFIDENCE_SUSPECT:
        findings.append("may be a genuine master low-pass filtered in mastering")
    reason = "; ".join(findings)

    return HiResCheckResult(
        file_path=str(path),
        declared_sample_rate=int(sr),
        total_duration_s=total_duration,
        analyzed_duration_s=analyzed_duration,
        cutoff_frequency_hz=cutoff,
        noise_floor_db=noise_floor_db,
        verdict=verdict,
        declared_bit_depth=declared_bits,
        effective_bit_depth=effective_bits,
        reason=reason,
        confidence=confidence,
        upsampling_artifact=artifact,
        brickwall_hz=brickwall_hz,
        noise_floor_class=floor_class,
        noise_floor_vs_16bit_db=floor_vs_16bit_db,
        music_cutoff_hz=music_cutoff_hz,
        ultrasonic_noise_only=ultrasonic_noise_only,
        useful_sample_rate=useful_sample_rate,
    )


async def check_file_async(
    file_path: str | Path,
    sample_seconds: int = 30,
    noise_floor_db: float = -80.0,
    hires_sample_rate_threshold: int = 48000,
    hires_cutoff_threshold_hz: float = 28000.0,
    n_fft: int = 4096,
) -> HiResCheckResult:
    """Async wrapper around :func:`check_file`.

    The decode and the FFT are CPU-bound and blocking, so this runs in
    a worker thread via `asyncio.to_thread` to avoid stalling the event
    loop (and, in turn, every other in-flight download).
    """
    return await asyncio.to_thread(
        check_file,
        file_path,
        sample_seconds=sample_seconds,
        noise_floor_db=noise_floor_db,
        hires_sample_rate_threshold=hires_sample_rate_threshold,
        hires_cutoff_threshold_hz=hires_cutoff_threshold_hz,
        n_fft=n_fft,
    )

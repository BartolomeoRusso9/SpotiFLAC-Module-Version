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

This is a best-effort heuristic, not a certification, and the thing it
measures cannot distinguish between the two ways a spectrum ends at 22 kHz:
an upsampled CD, and a genuine hi-res master that was deliberately low-pass
filtered during mastering (not rare in pop/rock). Both read as "fake_hires"
here, because in the signal they are the same. Treat the verdict as a hint
worth a closer listen, not proof — and note that acting on it automatically
(SpotiFLAC's --redownload-fake-hires) will replace such a master with a
LOSSLESS copy, which costs its bit depth even though no audible content is
lost.

Public API:
    - is_available() -> bool
    - check_file(path, ...) -> HiResCheckResult          (sync, blocking)
    - check_file_async(path, ...) -> HiResCheckResult     (off-thread)
    - HiResCheckError                                      (raised on failure)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("SpotiFLAC.hires_check")

try:
    import numpy as np
    import soundfile as sf

    _AUDIO_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - both are install dependencies
    np = None  # type: ignore[assignment]
    sf = None  # type: ignore[assignment]
    _AUDIO_IMPORT_ERROR = exc


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

    @property
    def is_suspicious(self) -> bool:
        """True only for a clear, high-confidence "fake hi-res" verdict."""
        return self.verdict == "fake_hires"

    @property
    def padded_bit_depth(self) -> bool:
        """True when the declared depth is real bits the file never uses."""
        return self.declared_bit_depth > 16 and 0 < self.effective_bit_depth <= 16

    def summary(self) -> str:
        labels = {
            "fake_hires": (
                f"LIKELY FAKE HI-RES — {self.reason}"
                if self.reason
                else "LIKELY FAKE HI-RES"
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


def _measure_bit_depth(
    path: Path,
    start_frame: int,
    frames: int,
) -> tuple[int, int]:
    """(declared, effective) bits per sample over one window of `path`.

    Effective depth is counted, not estimated: soundfile hands back every
    PCM subtype left-justified in an int32, so OR-ing the whole window
    together and counting the low bits that stayed zero says exactly how
    many bits the file ever puts to use. A 16-bit master padded into a
    24-bit container leaves its bottom 8 bits zero in every sample and is
    caught with certainty — there is no threshold here to tune or to argue
    with.

    Returns (0, 0) for anything with no fixed-point depth to check, and for
    any read failure: an unmeasurable depth must leave the spectral verdict
    exactly as it was, never turn into a finding of its own.
    """
    try:
        declared = _PCM_SUBTYPE_BITS.get(sf.info(path).subtype, 0)
    except Exception as exc:
        logger.debug("[hires-check] could not read subtype of '%s': %s", path, exc)
        return 0, 0
    if not declared:
        return 0, 0

    try:
        with sf.SoundFile(path) as handle:
            handle.seek(start_frame)
            window = handle.read(frames, dtype="int32", always_2d=True)
    except Exception as exc:
        logger.debug("[hires-check] could not read samples of '%s': %s", path, exc)
        return declared, 0

    if window.size == 0:
        return declared, 0

    accumulated = int(np.bitwise_or.reduce(window.astype(np.uint32).ravel()))
    if accumulated == 0:
        # Digital silence carries no bits at all. Reporting 0 keeps it out
        # of the padded-depth test rather than making every silent passage
        # look like a 16-bit fake.
        return declared, 0

    unused_low_bits = int((accumulated & -accumulated).bit_length() - 1)
    return declared, 32 - unused_low_bits


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


def _read_mono_window(
    path: Path,
    start_frame: int,
    frames: int,
) -> tuple["np.ndarray", int]:
    """The requested window of `path`, downmixed to mono, as float32.

    soundfile is what librosa itself decoded through, so this reads exactly
    the same formats it did — libsndfile's list, which is FLAC, WAV, AIFF,
    OGG, MP3 and friends, but *not* ALAC in an MP4 container.
    """
    with sf.SoundFile(path) as handle:
        handle.seek(start_frame)
        block = handle.read(frames, dtype="float32", always_2d=True)
        return block.mean(axis=1), int(handle.samplerate)


def _average_magnitude_spectrum(y: "np.ndarray", n_fft: int) -> "np.ndarray":
    """Mean magnitude across a Hann-windowed STFT of `y`.

    A hand-rolled equivalent of `np.abs(librosa.stft(y, n_fft)).mean(axis=1)`,
    matching its defaults exactly: a periodic Hann window, a hop of n_fft/4,
    and centred frames (the signal zero-padded by n_fft/2 at both ends).
    Checked against librosa on real and synthetic audio before librosa was
    dropped: the spectra agree to 1.1e-07 relative — float32 rounding, i.e.
    the same computation — and every cutoff came out identical.
    """
    hop = n_fft // 4
    # Annotated because numpy is `follow_imports = "skip"` for mypy (see
    # pyproject.toml), so everything it returns arrives as untyped Any.
    window: Any = np.hanning(n_fft + 1)[:-1].astype(np.float32)
    padded: Any = np.pad(y, n_fft // 2, mode="constant")
    frame_count = 1 + (len(padded) - n_fft) // hop
    if frame_count < 1:
        return np.zeros(n_fft // 2 + 1, dtype=np.float32)

    # One strided view rather than a Python loop over frames: a 30s window
    # at 176.4 kHz is ~5000 frames, and the copy this makes is bounded by
    # the window length the caller already agreed to hold in memory.
    starts: Any = hop * np.arange(frame_count)[:, None]
    frames = padded[starts + np.arange(n_fft)[None, :]] * window
    return np.abs(np.fft.rfft(frames, n=n_fft, axis=1)).mean(axis=0)


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
        file_path: Path to an audio file libsndfile can open — FLAC, WAV,
            AIFF, OGG, MP3 and the rest of its list. Notably *not* ALAC in
            an MP4 container, which raises HiResCheckError here and is
            reported by callers as "verification skipped". That was already
            true when this ran on librosa: recent versions dropped audioread
            and decode through soundfile, so both fail on an .m4a with the
            same libsndfile error.
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
        raise HiResCheckError(
            f"Could not read the audio header (unsupported or corrupt file?): {exc}"
        ) from exc
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
        y, sr = _read_mono_window(path, start_frame, window_frames)
    except Exception as exc:
        raise HiResCheckError(f"Could not decode audio: {exc}") from exc

    if y is None or getattr(y, "size", 0) == 0:
        raise HiResCheckError("Decoded audio segment is empty")
    if sr <= 0:
        raise HiResCheckError(f"Decoder returned an invalid sample rate: {sr}")

    # A fully-silent (or near-silent) segment makes spectral analysis
    # meaningless rather than wrong — report it as inconclusive instead of
    # guessing.
    if not np.any(np.abs(y) > 1e-9):
        return HiResCheckResult(
            file_path=str(path),
            declared_sample_rate=int(sr),
            total_duration_s=total_duration,
            analyzed_duration_s=analyzed_duration,
            cutoff_frequency_hz=0.0,
            noise_floor_db=noise_floor_db,
            verdict="inconclusive",
        )

    # Shrink n_fft for very short segments so a huge window is not padded
    # over a tiny signal, which would measure the padding as much as the
    # audio.
    effective_n_fft = n_fft
    while effective_n_fft > 256 and effective_n_fft > len(y) * 2:
        effective_n_fft //= 2

    try:
        avg_spectrum = _average_magnitude_spectrum(y, effective_n_fft)
        if avg_spectrum.size == 0:
            raise HiResCheckError("Spectral analysis produced no bins")
        peak = float(np.max(avg_spectrum))
        if peak <= 0.0:
            return HiResCheckResult(
                file_path=str(path),
                declared_sample_rate=int(sr),
                total_duration_s=total_duration,
                analyzed_duration_s=analyzed_duration,
                cutoff_frequency_hz=0.0,
                noise_floor_db=noise_floor_db,
                verdict="inconclusive",
            )
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

    declared_bits, effective_bits = _measure_bit_depth(
        path,
        start_frame=start_frame,
        frames=window_frames,
    )

    # A file can claim Hi-Res by rate, by depth, or by both, and each claim
    # is answered by the test that can actually judge it. Keeping them
    # separate is what lets a 24-bit/44.1 kHz file be judged at all: it
    # claims nothing by rate, so the spectral test has no opinion on it,
    # and treating "no opinion" as "nothing to flag" is how a padded CD
    # master used to pass without being looked at.
    claims_by_rate = sr > hires_sample_rate_threshold
    claims_by_depth = declared_bits > 16
    rate_is_fake = claims_by_rate and cutoff < hires_cutoff_threshold_hz
    depth_is_fake = claims_by_depth and 0 < effective_bits <= 16

    if not (claims_by_rate or claims_by_depth):
        verdict = "standard_definition"
    elif rate_is_fake or depth_is_fake:
        verdict = "fake_hires"
    else:
        verdict = "genuine_hires"

    findings = []
    if rate_is_fake:
        findings.append(f"declares {int(sr)} Hz but content stops at ~{cutoff:.0f} Hz")
    if depth_is_fake:
        findings.append(
            f"declares {declared_bits}-bit but only {effective_bits} bits carry data"
        )
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

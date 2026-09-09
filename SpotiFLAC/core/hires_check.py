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

logger = logging.getLogger("SpotiFLAC.hires_check")

try:
    import librosa
    import numpy as np
    import soundfile as sf

    _LIBROSA_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depends on optional install
    librosa = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    _LIBROSA_IMPORT_ERROR = exc


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
    """Whether the optional `librosa`/`numpy` dependencies are installed."""
    return _LIBROSA_IMPORT_ERROR is None


def _require_librosa() -> None:
    if _LIBROSA_IMPORT_ERROR is not None:
        raise HiResCheckError(
            "Hi-Res verification requires the optional 'librosa' and 'numpy' "
            "packages, which are not installed. Install them with: "
            "pip install librosa numpy  "
            "(or: pip install SpotiFLAC[hires]). "
            f"Original import error: {_LIBROSA_IMPORT_ERROR}"
        )


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
        file_path: Path to an audio file readable by librosa/soundfile
            (FLAC, WAV, ALAC/M4A, AIFF, MP3, ...).
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
            short segments to avoid librosa warnings/errors.

    Returns:
        A populated HiResCheckResult. Never returns partial/garbage data —
        any failure raises HiResCheckError instead.

    Raises:
        HiResCheckError: for any condition that prevents a reliable
            analysis (missing dependency, missing/empty/corrupt file,
            unreadable audio, invalid parameters, fully silent segment
            after decoding that also fails the safety net below).
    """
    _require_librosa()

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

    try:
        declared_sr = int(librosa.get_samplerate(path))
    except Exception as exc:
        raise HiResCheckError(
            f"Could not read sample rate (unsupported or corrupt file?): {exc}"
        ) from exc
    if declared_sr <= 0:
        raise HiResCheckError(f"Invalid declared sample rate: {declared_sr}")

    try:
        total_duration = float(librosa.get_duration(path=path))
    except Exception as exc:
        raise HiResCheckError(f"Could not read duration: {exc}") from exc
    if total_duration <= 0:
        raise HiResCheckError(
            "File reports zero or negative duration — likely corrupt/unreadable"
        )

    analyzed_duration = min(float(sample_seconds), total_duration)
    offset = max(0.0, (total_duration - analyzed_duration) / 2)

    try:
        y, sr = librosa.load(
            path,
            sr=None,  # keep the file's native sample rate
            mono=True,
            offset=offset,
            duration=analyzed_duration,
        )
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

    # Shrink n_fft for very short segments so librosa doesn't pad a huge
    # window over a tiny signal (also avoids its "n_fft too large" warning).
    effective_n_fft = n_fft
    while effective_n_fft > 256 and effective_n_fft > len(y) * 2:
        effective_n_fft //= 2

    try:
        spectrogram = np.abs(librosa.stft(y, n_fft=effective_n_fft))
        if spectrogram.size == 0:
            raise HiResCheckError("STFT produced an empty spectrogram")
        avg_spectrum = np.mean(spectrogram, axis=1)
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
        # top_db=None matters more than it looks. librosa's default clamps
        # everything to `peak - 80 dB`, which is exactly where
        # noise_floor_db also sits by default — so every clamped bin
        # compared as "active" against any floor below -80, and the check
        # reported the full Nyquist frequency as the cutoff for every file
        # it was given. Documented as tunable, the parameter silently
        # disabled the check at any value under its own default.
        spectrum_db = librosa.amplitude_to_db(avg_spectrum, ref=np.max, top_db=None)
        frequencies = librosa.fft_frequencies(sr=sr, n_fft=effective_n_fft)
    except HiResCheckError:
        raise
    except Exception as exc:
        raise HiResCheckError(f"Spectral analysis failed: {exc}") from exc

    active = frequencies[spectrum_db > noise_floor_db]
    cutoff = float(active[-1]) if active.size else 0.0

    declared_bits, effective_bits = _measure_bit_depth(
        path,
        start_frame=int(offset * sr),
        frames=int(analyzed_duration * sr),
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

    librosa/numpy are CPU-bound and blocking, so this runs the analysis in
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

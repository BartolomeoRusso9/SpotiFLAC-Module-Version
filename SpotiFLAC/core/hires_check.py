"""Hi-Res authenticity checker.

Detects "fake hi-res" audio files: files that declare a high sample rate
(e.g. 96 kHz / 24-bit) but whose actual spectral content stops at, or just
above, the ~22.05 kHz Nyquist limit of a standard 44.1 kHz source. That kind
of sharp cutoff is a common fingerprint of upsampling — taking a CD-quality
or lossy source and re-encoding it at a higher sample rate without adding
any real high-frequency content, to make it *look* like Hi-Res.

This is a best-effort heuristic, not a certification. Some genuinely hi-res
masters are deliberately low-pass filtered during mastering (common in pop/
rock) and will still read as "no anomaly" here, while an unusual but
legitimate mix could occasionally look suspicious. Treat a "fake_hires"
verdict as a hint worth a closer listen, not definitive proof.

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

    @property
    def is_suspicious(self) -> bool:
        """True only for a clear, high-confidence "fake hi-res" verdict."""
        return self.verdict == "fake_hires"

    def summary(self) -> str:
        labels = {
            "fake_hires": (
                "LIKELY FAKE HI-RES — sharp spectral cutoff detected "
                "(possible upsampling from a lower-resolution source)"
            ),
            "standard_definition": (
                "Standard-definition file — nothing to flag "
                "(declared sample rate does not claim Hi-Res)"
            ),
            "genuine_hires": (
                "Spectral content extends past CD/DVD limits — looks like genuine Hi-Res"
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
            f"  Verdict              : {labels.get(self.verdict, self.verdict)}"
        )


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
    hires_cutoff_threshold_hz: float = 24000.0,
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
            (Hz) a genuine Hi-Res file is expected to reach.
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
        spectrum_db = librosa.amplitude_to_db(avg_spectrum, ref=np.max)
        frequencies = librosa.fft_frequencies(sr=sr, n_fft=effective_n_fft)
    except HiResCheckError:
        raise
    except Exception as exc:
        raise HiResCheckError(f"Spectral analysis failed: {exc}") from exc

    active = frequencies[spectrum_db > noise_floor_db]
    cutoff = float(active[-1]) if active.size else 0.0

    if sr > hires_sample_rate_threshold and cutoff < hires_cutoff_threshold_hz:
        verdict = "fake_hires"
    elif sr <= hires_sample_rate_threshold:
        verdict = "standard_definition"
    else:
        verdict = "genuine_hires"

    return HiResCheckResult(
        file_path=str(path),
        declared_sample_rate=int(sr),
        total_duration_s=total_duration,
        analyzed_duration_s=analyzed_duration,
        cutoff_frequency_hz=cutoff,
        noise_floor_db=noise_floor_db,
        verdict=verdict,
    )


async def check_file_async(
    file_path: str | Path,
    sample_seconds: int = 30,
    noise_floor_db: float = -80.0,
    hires_sample_rate_threshold: int = 48000,
    hires_cutoff_threshold_hz: float = 24000.0,
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

#!/usr/bin/env python3
"""Standalone command-line tool: Hi-Res Spectrum Analyzer.

Detects "fake Hi-Res" audio files — files that declare a high sample rate
but contain no real content above the Nyquist limit of a standard-definition
source (a common fingerprint of upsampling).

Usage:
    python -m SpotiFLAC.tools.hires_check_cli FILE [--seconds N]
    python -m SpotiFLAC.tools.hires_check_cli FILE1 FILE2 FILE3
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

# Keep the terminal clean of unrelated third-party warnings (e.g. from
# audioread/numba) without silencing our own error reporting below.
warnings.filterwarnings("ignore")

from SpotiFLAC.core.hires_check import (
    HiResCheckError,
    check_file,
    is_available,
)

EXIT_OK = 0
EXIT_SUSPICIOUS_FOUND = 1
EXIT_DEPENDENCY_MISSING = 2
EXIT_ALL_FAILED = 3


def _analyze_one(file_path: str, sample_seconds: int) -> tuple[bool, bool]:
    """Analyzes a single file.

    Returns:
        (analyzed_ok, is_suspicious)
    """
    path = Path(file_path)
    print(f"Analyzing '{path.name}'...")

    try:
        result = check_file(path, sample_seconds=sample_seconds)
    except HiResCheckError as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return False, False

    print(f"  Declared sample rate : {result.declared_sample_rate} Hz")
    print(
        f"  Analyzed segment     : {result.analyzed_duration_s:.1f}s "
        f"(of {result.total_duration_s:.1f}s total)"
    )
    print(f"  Active cutoff freq.  : ~{result.cutoff_frequency_hz:.0f} Hz")

    icons = {
        "fake_hires": "\u26a0\ufe0f",
        "standard_definition": "\u2139\ufe0f",
        "genuine_hires": "\u2705",
        "inconclusive": "\u2754",
    }
    icon = icons.get(result.verdict, "")
    print(f"  {icon} {result.summary().splitlines()[-1].split(': ', 1)[-1]}")
    print()

    return True, result.is_suspicious


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Spectrum analyzer for detecting fake Hi-Res audio files.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="One or more audio file paths to analyze",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=30,
        dest="sample_seconds",
        help="Seconds of audio to extract and analyze from the middle of "
        "each track (default: 30)",
    )

    args = parser.parse_args(argv)

    if not is_available():
        print(
            "Error: Hi-Res verification requires the optional 'librosa' and "
            "'numpy' packages, which are not installed.\n"
            "Install them with: pip install librosa numpy\n"
            "(or: pip install SpotiFLAC[hires])",
            file=sys.stderr,
        )
        return EXIT_DEPENDENCY_MISSING

    if args.sample_seconds <= 0:
        print("Error: --seconds must be a positive integer.", file=sys.stderr)
        return EXIT_ALL_FAILED

    analyzed_count = 0
    suspicious_count = 0

    for file_path in args.files:
        ok, suspicious = _analyze_one(file_path, args.sample_seconds)
        if ok:
            analyzed_count += 1
            if suspicious:
                suspicious_count += 1

    if analyzed_count == 0:
        print("No file could be analyzed.", file=sys.stderr)
        return EXIT_ALL_FAILED

    if len(args.files) > 1:
        print(f"Analyzed {analyzed_count}/{len(args.files)} file(s).")
        if suspicious_count:
            print(f"{suspicious_count} file(s) flagged as likely fake Hi-Res.")

    return EXIT_SUSPICIOUS_FOUND if suspicious_count else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

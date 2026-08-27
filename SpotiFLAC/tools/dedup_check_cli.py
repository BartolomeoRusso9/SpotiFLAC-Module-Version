#!/usr/bin/env python3
"""Standalone command-line tool: acoustic-fingerprint duplicate finder.

Scans a folder for audio files that are the same recording (ISRC/tags
disagreeing or missing entirely, or simply not compared before), using
Chromaprint acoustic fingerprints rather than metadata — see
core/audio_fingerprint.py for how the comparison itself works.

Usage:
    python -m SpotiFLAC.tools.dedup_check_cli FOLDER [--tolerance SECONDS]
                                                       [--threshold 0.0-1.0]
                                                       [--recursive / --no-recursive]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from SpotiFLAC.core.audio_fingerprint import (
    AudioFingerprintError,
    compute_fingerprint,
    find_duplicate_groups,
    is_available,
)
from SpotiFLAC.core.local_scanner import SUPPORTED_EXTENSIONS

EXIT_OK = 0
EXIT_DUPLICATES_FOUND = 1
EXIT_DEPENDENCY_MISSING = 2
EXIT_NO_FILES = 3


def _iter_audio_files(folder: Path, recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(
        f
        for f in folder.glob(pattern)
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find duplicate audio files by acoustic fingerprint, "
        "not by filename or tags.",
    )
    parser.add_argument("folder", metavar="FOLDER", help="Folder to scan")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="Max duration difference to still consider two files "
        "candidates for comparison (default: 3.0)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        metavar="0.0-1.0",
        help="Minimum fingerprint similarity to call two files duplicates "
        "(default: 0.95)",
    )
    parser.add_argument(
        "--recursive",
        dest="recursive",
        action="store_true",
        default=True,
        help="Scan subfolders too (default)",
    )
    parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="Only scan the given folder itself",
    )
    args = parser.parse_args(argv)

    if not is_available():
        print(
            "Error: duplicate detection requires the optional 'pyacoustid' "
            "package and the 'fpcalc' binary (from Chromaprint), neither of "
            "which is installed.\n"
            "Install with: pip install SpotiFLAC[dedup]\n"
            "then install fpcalc — see https://acoustid.org/chromaprint "
            "(most package managers ship it as 'chromaprint' or "
            "'libchromaprint-tools').",
            file=sys.stderr,
        )
        return EXIT_DEPENDENCY_MISSING

    folder = Path(args.folder)
    files = _iter_audio_files(folder, args.recursive)
    if not files:
        print(f"No supported audio files found under {folder}", file=sys.stderr)
        return EXIT_NO_FILES

    print(f"Fingerprinting {len(files)} file(s)…")
    fingerprints = []
    for f in files:
        try:
            fingerprints.append(compute_fingerprint(f))
        except AudioFingerprintError as exc:
            print(f"  skipped {f.name}: {exc}", file=sys.stderr)

    if not fingerprints:
        print("No file could be fingerprinted.", file=sys.stderr)
        return EXIT_NO_FILES

    groups = find_duplicate_groups(
        fingerprints,
        duration_tolerance_s=args.tolerance,
        similarity_threshold=args.threshold,
    )

    if not groups:
        print(f"No duplicates found among {len(fingerprints)} file(s).")
        return EXIT_OK

    print(f"\nFound {len(groups)} duplicate group(s):\n")
    for i, group in enumerate(groups, 1):
        print(f"Group {i} ({len(group)} files):")
        for path in sorted(group):
            print(f"  - {path}")
        print()

    return EXIT_DUPLICATES_FOUND


if __name__ == "__main__":
    sys.exit(main())

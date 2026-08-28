"""tools/library_upgrade_cli.py — the `--upgrade-library` command.

Reporting and argument handling only; every decision about what counts as
upgradable lives in core/library_upgrade.py, and the fetching is the ordinary
download path. Same split as tools/hires_check_cli.py.

The default is a dry run. An upgrade re-downloads files and (with
`--replace`) overwrites what is already on disk, which is not something a
command should do because you were curious what it would find.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ..core.library_upgrade import ScanReport, scan_library


def format_report(report: ScanReport, *, verbose: bool = False) -> str:
    lines = [report.summary()]
    if not report.candidates:
        lines.append("\nNothing to upgrade.")
        return "\n".join(lines)

    lines.append("")
    for candidate in report.candidates:
        label = candidate.search_query or candidate.file_path
        lines.append(f"  {label}")
        lines.append(f"      {candidate.quality.describe()}  —  {candidate.reason}")
        if verbose:
            lines.append(f"      {candidate.file_path}")
    return "\n".join(lines)


def _progress(done: int, total: int, path: str) -> None:
    # Carriage return rather than a new line per file: a library scan is
    # thousands of files and this is a progress indicator, not a log.
    print(f"\r  scanning {done}/{total}…", end="", file=sys.stderr, flush=True)
    if done == total:
        print("", file=sys.stderr)


def run(
    path: str,
    target_quality: str = "LOSSLESS",
    *,
    recursive: bool = True,
    verify_hires: bool = False,
    as_json: bool = False,
    verbose: bool = False,
    show_progress: bool = True,
) -> ScanReport:
    report = scan_library(
        path,
        target_quality,
        recursive=recursive,
        verify_hires=verify_hires,
        progress=_progress if (show_progress and not as_json) else None,
    )
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_report(report, verbose=verbose))
    return report


async def run_and_upgrade_async(
    path: str,
    target_quality: str,
    download: Any,
    *,
    recursive: bool = True,
    verify_hires: bool = False,
    limit: int | None = None,
    as_json: bool = False,
    verbose: bool = False,
) -> int:
    """Scans, resolves each candidate to a Spotify URL, and downloads it.

    `download(url)` is injected — the launcher passes a closure over the
    ordinary download path, so an upgraded file lands with exactly the
    naming, tagging and provider order a manual download would have used.

    Returns the number of tracks dispatched.
    """
    from ..core.library_upgrade import plan_async

    report = run(
        path,
        target_quality,
        recursive=recursive,
        verify_hires=verify_hires,
        as_json=False,
        verbose=verbose,
    )
    if not report.candidates:
        return 0

    pairs = await plan_async(report, limit=limit)
    resolved = [(c, u) for c, u in pairs if u]
    unresolved = [c for c, u in pairs if not u]

    if unresolved:
        print(f"\nCould not find {len(unresolved)} of these on Spotify:")
        for candidate in unresolved[:20]:
            print(f"  · {candidate.search_query or candidate.file_path}")
        if len(unresolved) > 20:
            print(f"  … and {len(unresolved) - 20} more")

    print(f"\nUpgrading {len(resolved)} track(s)…")
    dispatched = 0
    for candidate, url in resolved:
        try:
            await download(url)
            dispatched += 1
        except Exception as exc:
            # One track that cannot be fetched must not end the run — the
            # remaining files are still upgradable.
            print(
                f"  ✗ {candidate.search_query or candidate.file_path}: {exc}",
                file=sys.stderr,
            )

    if as_json:
        print(
            json.dumps(
                {
                    **report.to_dict(),
                    "resolved": len(resolved),
                    "unresolved": len(unresolved),
                    "dispatched": dispatched,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    return dispatched

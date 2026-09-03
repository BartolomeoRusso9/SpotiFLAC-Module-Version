"""tools/library_dedup_cli.py — the `--dedup-library` command.

Reporting and argument handling only; every decision about what is a
duplicate and which copy survives lives in core/library_dedup.py. Same
split as tools/library_upgrade_cli.py and tools/hires_check_cli.py.

Also runnable on its own, for a library on a machine that has the package
but no reason to run the GUI:

    python -m SpotiFLAC.tools.library_dedup_cli /music --verify
    python -m SpotiFLAC.tools.library_dedup_cli /music --apply
    python -m SpotiFLAC.tools.library_dedup_cli --restore /music/.spotiflac-duplicates/dedup-….json

The default is a report. `--apply` quarantines; only `--delete --apply`
unlinks anything, which is two flags for one irreversible act on purpose.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..core.library_dedup import (
    ACTION_DELETE,
    ACTION_TRASH,
    DEFAULT_DURATION_TOLERANCE_S,
    DEFAULT_SIMILARITY_THRESHOLD,
    MATCH_BOTH,
    DedupReport,
    ResolutionResult,
    export_sqlite,
    human_size,
    load_report,
    resolve_duplicates,
    restore_manifest,
    scan_duplicates,
)

EXIT_OK = 0
EXIT_DUPLICATES_FOUND = 1
EXIT_NO_FILES = 3

#: Groups printed in full before the listing is summarised. A library with
#: four thousand duplicate groups is exactly the library whose report must
#: not itself be four thousand screens long; --verbose prints all of them.
_MAX_GROUPS_SHOWN = 40


def _progress(done: int, total: int, path: str) -> None:
    # Carriage return rather than a line per file: a library scan is
    # thousands of files and this is a progress indicator, not a log.
    print(f"\r  scanning {done}/{total}…", end="", file=sys.stderr, flush=True)
    if done == total:
        print("", file=sys.stderr)


def format_report(report: DedupReport, *, verbose: bool = False) -> str:
    lines = [report.summary()]
    if not report.groups:
        lines.append("\nNo duplicates found.")
        return "\n".join(lines)

    shown = report.groups if verbose else report.groups[:_MAX_GROUPS_SHOWN]
    lines.append("")
    for index, group in enumerate(shown, start=1):
        lines.append(
            f"{index}. {group.label}  "
            f"[{len(group.files)} copies, {human_size(group.reclaimable_bytes)} "
            f"reclaimable, matched by {group.matched_by}]"
        )
        lines.append(f"    keep · {group.keeper.path}")
        lines.append(f"           {group.keeper.describe()}")
        for duplicate in group.duplicates:
            lines.append(f"    drop · {duplicate.path}")
            lines.append(f"           {duplicate.describe()}")
        lines.append("")

    if len(shown) < len(report.groups):
        lines.append(
            f"… and {len(report.groups) - len(shown)} more group(s). "
            "Use --verbose to list them all, or --json for the lot."
        )
    return "\n".join(lines)


def format_resolution(result: ResolutionResult, *, verbose: bool = False) -> str:
    lines: list[str] = []
    resolved = result.resolved
    shown = resolved if verbose else resolved[:_MAX_GROUPS_SHOWN]
    for action in shown:
        arrow = f"  → {action.destination}" if action.destination else ""
        lines.append(f"  {action.action} · {action.path}{arrow}")
    if len(shown) < len(resolved):
        lines.append(f"  … and {len(resolved) - len(shown)} more")

    if result.skipped:
        lines.append("")
        lines.append("Left alone:")
        for action in result.skipped[:_MAX_GROUPS_SHOWN]:
            lines.append(f"  · {action.path}: {action.error}")
        if len(result.skipped) > _MAX_GROUPS_SHOWN:
            lines.append(f"  … and {len(result.skipped) - _MAX_GROUPS_SHOWN} more")

    lines.append("")
    lines.append(result.summary())
    return "\n".join(lines)


def run(
    path: str,
    *,
    recursive: bool = True,
    match: str = MATCH_BOTH,
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
    keep_version_noise: bool = False,
    verify: bool = False,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    use_cache: bool = True,
    apply: bool = False,
    delete: bool = False,
    trash_dir: str | None = None,
    limit: int | None = None,
    db_path: str | None = None,
    from_db: str | None = None,
    as_json: bool = False,
    verbose: bool = False,
    show_progress: bool = True,
) -> int:
    """Scans, reports, and — with `apply` — resolves. Returns an exit code.

    With `from_db` the scan is skipped and the report is read back from a
    database an earlier run wrote, which is how the walk and the resolution
    can happen on different machines. Nothing else changes: the resolver
    re-checks every file against what the database recorded before touching
    it, so a stale index skips files rather than removing the wrong ones.
    """
    if from_db:
        try:
            report = load_report(from_db)
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_NO_FILES
    else:
        report = scan_duplicates(
            path,
            recursive=recursive,
            match=match,
            duration_tolerance_s=duration_tolerance_s,
            strip_version_noise=not keep_version_noise,
            verify=verify,
            similarity_threshold=threshold,
            use_cache=use_cache,
            progress=_progress if (show_progress and not as_json) else None,
        )

    written = ""
    if db_path:
        try:
            written = str(export_sqlite(report, db_path))
        except OSError as exc:
            print(f"Error: could not write {db_path}: {exc}", file=sys.stderr)
            return EXIT_NO_FILES

    if not report.stats.files:
        if as_json:
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(f"No supported audio files found under {path}", file=sys.stderr)
        return EXIT_NO_FILES

    action = ACTION_DELETE if delete else ACTION_TRASH
    result = resolve_duplicates(
        report,
        action=action,
        trash_dir=trash_dir,
        dry_run=not apply,
        limit=limit,
    )

    if as_json:
        print(
            json.dumps(
                {
                    **report.to_dict(),
                    "database": written,
                    "resolution": result.to_dict(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(format_report(report, verbose=verbose))
        if written:
            print(f"\nIndex written to {written} ({report.stats.files} file(s)).")
        if report.groups:
            print()
            print(format_resolution(result, verbose=verbose))
            if not apply:
                print(
                    "\nNothing was changed. Re-run with --apply to move these "
                    "into the quarantine folder (--delete --apply to remove "
                    "them outright)."
                )

    return EXIT_DUPLICATES_FOUND if report.groups else EXIT_OK


def run_restore(
    manifest: str,
    *,
    dry_run: bool = False,
    as_json: bool = False,
    verbose: bool = False,
) -> int:
    try:
        result = restore_manifest(manifest, dry_run=dry_run)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_NO_FILES

    if as_json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_resolution(result, verbose=verbose))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m SpotiFLAC.tools.library_dedup_cli",
        description="Find — and optionally resolve — duplicate recordings in "
        "a music library.",
    )
    parser.add_argument(
        "folder", metavar="FOLDER", nargs="?", help="Library folder to scan"
    )
    parser.add_argument(
        "--match",
        choices=("isrc", "tags", "both"),
        default=MATCH_BOTH,
        help="isrc: only files whose ISRC agrees (safest). tags: only "
        "artist/title+duration. both (default): ISRC first, tags for the rest.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE_S,
        metavar="SECONDS",
        help="How far two durations may differ and still be the same "
        f"recording (default: {DEFAULT_DURATION_TOLERANCE_S})",
    )
    parser.add_argument(
        "--keep-version-noise",
        action="store_true",
        help="Treat '(2011 Remaster)' and the like as part of the title, so "
        "a remaster is not a duplicate of the original.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Confirm each group against the audio itself with Chromaprint "
        "before offering it. Needs the 'dedup' extra; slower, and the only "
        "mode that catches two files that share tags but not audio.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_SIMILARITY_THRESHOLD,
        metavar="0.0-1.0",
        help=f"Fingerprint similarity for --verify (default: {DEFAULT_SIMILARITY_THRESHOLD})",
    )
    parser.add_argument(
        "--no-cache",
        dest="use_cache",
        action="store_false",
        help="Re-read every file instead of reusing the per-file scan cache.",
    )
    parser.add_argument(
        "--no-recursive", dest="recursive", action="store_false", default=True
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually resolve the duplicates. Without it the command only reports.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="With --apply, unlink the redundant copies instead of moving "
        "them to the quarantine folder. Not undoable.",
    )
    parser.add_argument(
        "--trash-dir",
        default=None,
        metavar="DIR",
        help="Where quarantined copies go (default: .spotiflac-duplicates "
        "inside the scanned folder).",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Resolve at most N files."
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        metavar="FILE.db",
        help="Also write the whole scan to a SQLite database: one row per "
        "file, duplicate groups on top. Readable by anything that speaks "
        "SQLite, and by --from-db.",
    )
    parser.add_argument(
        "--from-db",
        dest="from_db",
        default=None,
        metavar="FILE.db",
        help="Skip the scan and read the report back from a database a "
        "previous run wrote — the walk and the resolution need not happen "
        "on the same machine.",
    )
    parser.add_argument(
        "--restore",
        metavar="MANIFEST",
        default=None,
        help="Undo a previous --apply run from the manifest it wrote.",
    )
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.restore:
        return run_restore(args.restore, as_json=args.as_json, verbose=args.verbose)

    if not args.folder and not args.from_db:
        print(
            "Error: a FOLDER is required (or --from-db FILE.db, or "
            "--restore MANIFEST)",
            file=sys.stderr,
        )
        return EXIT_NO_FILES

    return run(
        args.folder or "",
        recursive=args.recursive,
        match=args.match,
        duration_tolerance_s=args.tolerance,
        keep_version_noise=args.keep_version_noise,
        verify=args.verify,
        threshold=args.threshold,
        use_cache=args.use_cache,
        apply=args.apply,
        delete=args.delete,
        trash_dir=args.trash_dir,
        limit=args.limit,
        db_path=args.db_path,
        from_db=args.from_db,
        as_json=args.as_json,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())

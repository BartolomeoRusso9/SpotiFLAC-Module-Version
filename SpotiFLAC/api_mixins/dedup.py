"""api_mixins/dedup.py — the duplicate finders' GUI/web surface.

Two of them, for two sizes of question:

  - `scan_for_duplicates()` fingerprints a folder outright (see
    core/audio_fingerprint.py). Exact, and quadratic — the right answer for
    a folder, the wrong one for a library.
  - `scan_library_duplicates()` runs the metadata pass in
    core/library_dedup.py, which is what scales to a real library, and can
    then *resolve* what it found: quarantine, delete, restore.

Both mirror LocalTaggingMixin.scan_local()'s shape — background thread,
results delivered via a push event — since both are folder scans that take
a while and must not block the UI thread.

The resolving half keeps the last report on the instance. That is the same
place `current_tracks` lives and has the same lifetime (one instance per
account in --web-multiuser mode), and it is what lets the UI say "remove
these three" without re-walking the library to find out what they were.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

#: Groups pushed to the frontend in one message. A library can produce
#: thousands, and a browser handed all of them at once has to parse and lay
#: out megabytes before it can show the first one. The report keeps the lot;
#: the UI says how many it is not showing.
_MAX_GROUPS_PUSHED = 500


class DedupMixin:
    def _approved_path(self, path: str) -> tuple[str, str]:
        """(usable path, error). Empty error means the path may be used.

        The same approved-roots confinement scan_local() applies: a request
        must not be able to point a scan — let alone a *deletion* — at an
        arbitrary place on the filesystem.
        """
        if not path:
            return "", "No path given"
        path = os.path.expanduser(path.strip().strip("'\""))
        if not os.path.exists(path):
            return "", f"Path does not exist: {path}"
        try:
            resolved = Path(path).resolve()
            approved_roots = [Path(self.download_dir).resolve(), Path.home().resolve()]
            if not any(_is_within(resolved, root) for root in approved_roots):
                return "", "Access denied: path is outside approved directories"
        except Exception as e:
            return "", f"Path validation failed: {e}"
        return path, ""

    def get_dedup_status(self) -> dict:
        """Whether duplicate detection can run at all on this machine —
        call this before offering the feature in the UI, same idea as
        get_ffmpeg_status().
        """
        try:
            # can_compare(), not is_available(): grouping duplicates needs
            # decoded fingerprints, which needs libchromaprint on top of the
            # fpcalc binary. Identifying a single file (acoustid_lookup) has
            # the lower bar and is checked separately.
            from ..core.audio_fingerprint import can_compare

            available = can_compare()
        except Exception:
            available = False
        return {
            "available": available,
            "install_hint": (
                None
                if available
                else "pip install SpotiFLAC[dedup], plus Chromaprint itself: "
                "the 'fpcalc' binary on PATH and the libchromaprint shared "
                "library (Homebrew's chromaprint formula installs fpcalc only)"
            ),
        }

    def scan_for_duplicates(
        self, path: str, recursive: bool = True, threshold: float = 0.95
    ) -> dict:
        """Scans `path` for audio files that are acoustically the same
        recording, in a background thread. Results arrive via the
        'app_dedup_results' push event as
        {"groups": [[file_path, ...], ...]}; 'app_dedup_error' on failure.
        """
        path, error = self._approved_path(path)
        if error:
            return {"status": "error", "error": error}

        threading.Thread(
            target=self._scan_for_duplicates_thread,
            args=(path, recursive, threshold),
            daemon=True,
        ).start()
        return {"status": "started"}

    def _scan_for_duplicates_thread(
        self, path: str, recursive: bool, threshold: float
    ) -> None:
        try:
            from pathlib import Path

            from ..core.audio_fingerprint import (
                AudioFingerprintError,
                can_compare,
                compute_fingerprint,
                find_duplicate_groups,
            )
            from ..core.local_scanner import SUPPORTED_EXTENSIONS

            # can_compare(), not is_available(): with fpcalc but no
            # libchromaprint every fingerprint comes back with an empty
            # `raw`, so the scan would run to completion and report zero
            # duplicates — a silently wrong answer, which is worse than
            # saying the feature is unavailable.
            if not can_compare():
                self._push(
                    "app_dedup_error",
                    "Duplicate detection requires the optional 'pyacoustid' "
                    "package, the 'fpcalc' binary AND the libchromaprint "
                    "library — see get_dedup_status().",
                )
                return

            p = Path(path)
            files = (
                sorted(
                    f
                    for f in p.glob("**/*" if recursive else "*")
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
                )
                if p.is_dir()
                else [p]
            )

            fingerprints = []
            skipped = 0
            for f in files:
                try:
                    fingerprints.append(compute_fingerprint(f))
                except AudioFingerprintError as e:
                    # Quiet: a scan over a large library can skip a lot of
                    # files, and one toast each would bury the window. Each
                    # is still named in the Logs view; the count below is
                    # what the user is told up front.
                    skipped += 1
                    self.log(f"[dedup] skipped {f.name}: {e}", "warn-quiet")
            if skipped:
                self.log(
                    f"[dedup] skipped {skipped} unreadable file(s) — "
                    "see the Logs view for which.",
                    "warn",
                )

            groups = find_duplicate_groups(fingerprints, similarity_threshold=threshold)
            self._push(
                "app_dedup_results",
                {"groups": [[str(p) for p in g] for g in groups]},
            )
        except Exception as e:
            self.log(f"[dedup] scan failed: {e}", "error")
            self._push("app_dedup_error", str(e))

    # ── Library-wide duplicates (core/library_dedup.py) ─────────────────────

    def scan_library_duplicates(
        self,
        path: str,
        recursive: bool = True,
        match: str = "both",
        tolerance: float = 4.0,
        verify: bool = False,
        threshold: float = 0.95,
        export_db: bool = False,
    ) -> dict:
        """Scans a whole library for duplicate recordings, in a background
        thread. Results arrive via the 'app_library_dedup_results' push
        event as the report's to_dict(); 'app_library_dedup_error' on
        failure.

        Nothing is removed here. The report names a keeper per group, and
        resolve_library_duplicates() is the separate call that acts on it —
        two decisions, as on the command line.
        """
        path, error = self._approved_path(path)
        if error:
            return {"status": "error", "error": error}
        if match not in ("isrc", "tags", "both"):
            return {"status": "error", "error": f"Unknown match mode: {match}"}

        threading.Thread(
            target=self._scan_library_duplicates_thread,
            args=(path, recursive, match, tolerance, verify, threshold, export_db),
            daemon=True,
        ).start()
        return {"status": "started"}

    def _scan_library_duplicates_thread(
        self,
        path: str,
        recursive: bool,
        match: str,
        tolerance: float,
        verify: bool,
        threshold: float,
        export_db: bool,
    ) -> None:
        try:
            from ..core.library_dedup import (
                TRASH_DIRNAME,
                export_sqlite,
                scan_duplicates,
            )

            def progress(done: int, total: int, current: str) -> None:
                # One event per file would be thousands of WebSocket frames
                # for a library; the UI only needs to see the bar move.
                if done % 250 == 0 or done == total:
                    self._push(
                        "app_library_dedup_progress",
                        {"done": done, "total": total},
                    )

            report = scan_duplicates(
                path,
                recursive=recursive,
                match=match,
                duration_tolerance_s=float(tolerance),
                verify=bool(verify),
                similarity_threshold=float(threshold),
                progress=progress,
            )
            self._library_dedup_report = report

            database = ""
            if export_db:
                try:
                    database = str(
                        export_sqlite(
                            report, Path(path) / TRASH_DIRNAME / "library-index.db"
                        )
                    )
                except OSError as e:
                    # The index is a convenience; the scan it describes is
                    # not, and it has already succeeded by this point.
                    self.log(f"[dedup] could not write the index: {e}", "warn")

            payload = report.to_dict()
            payload["duplicate_groups"] = payload["duplicate_groups"][
                :_MAX_GROUPS_PUSHED
            ]
            payload["shown_groups"] = len(payload["duplicate_groups"])
            payload["database"] = database
            self._push("app_library_dedup_results", payload)
        except Exception as e:
            self.log(f"[dedup] library scan failed: {e}", "error")
            self._push("app_library_dedup_error", str(e))

    def resolve_library_duplicates(
        self,
        paths: list | None = None,
        keep_paths: list | None = None,
        action: str = "trash",
        dry_run: bool = False,
    ) -> dict:
        """Resolves duplicates from the last scan_library_duplicates() run.

        `paths` are the redundant copies to act on — what the UI's
        checkboxes selected; omitted, every duplicate in the report is
        acted on. `keep_paths` overrides which copy survives in the groups
        it names.

        Deliberately synchronous: it is a filesystem move per file, it is
        fast, and its result is the confirmation the UI has to show before
        the user believes their library changed.
        """
        report = getattr(self, "_library_dedup_report", None)
        if report is None:
            return {
                "status": "error",
                "error": "No scan to resolve — run a library duplicate scan first.",
            }
        if action not in ("trash", "delete"):
            return {"status": "error", "error": f"Unknown action: {action}"}

        # The paths are the ones the last scan reported, which were found
        # under an approved root; anything else the request made up is not
        # in the report and cannot be selected into the run.
        known = {f.path for group in report.groups for f in group.duplicates}
        selected = {p for p in (paths or []) if p in known} if paths else None
        if paths and not selected:
            return {
                "status": "error",
                "error": "None of those files are duplicates in the current scan.",
            }

        try:
            from ..core.library_dedup import resolve_duplicates

            result = resolve_duplicates(
                report,
                action=action,
                dry_run=bool(dry_run),
                keep_paths=set(keep_paths or []) or None,
                only_paths=selected,
            )
        except Exception as e:
            self.log(f"[dedup] resolve failed: {e}", "error")
            return {"status": "error", "error": str(e)}

        if not dry_run and result.resolved:
            # The report describes a library that no longer exists: acting
            # on it twice would report every file it just moved as "gone
            # since the scan". A fresh scan is the only honest next step.
            self._library_dedup_report = None
            self.log(
                f"[dedup] {len(result.resolved)} file(s) resolved, "
                f"{result.action}; manifest: {result.manifest_path or 'none'}",
                "info",
            )
        return {"status": "ok", **result.to_dict()}

    def restore_library_duplicates(self, manifest: str) -> dict:
        """Undoes a quarantine run from the manifest it wrote."""
        manifest, error = self._approved_path(manifest)
        if error:
            return {"status": "error", "error": error}
        try:
            from ..core.library_dedup import restore_manifest

            result = restore_manifest(manifest)
        except (OSError, ValueError) as e:
            return {"status": "error", "error": str(e)}
        self._library_dedup_report = None
        return {"status": "ok", **result.to_dict()}


def _is_within(path, root) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

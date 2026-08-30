"""api_mixins/dedup.py — acoustic-fingerprint duplicate finder, GUI/web surface.

See core/audio_fingerprint.py for how the comparison itself works and why
it's off by default. Mirrors LocalTaggingMixin.scan_local()'s shape
(background thread, results delivered via a push event) since this is the
same kind of operation: a folder scan that can take a while and shouldn't
block the UI thread.
"""

from __future__ import annotations

import threading


class DedupMixin:
    def get_dedup_status(self) -> dict:
        """Whether duplicate detection can run at all on this machine —
        call this before offering the feature in the UI, same idea as
        get_ffmpeg_status().
        """
        try:
            from ..core.audio_fingerprint import is_available

            available = is_available()
        except Exception:
            available = False
        return {
            "available": available,
            "install_hint": (
                None
                if available
                else "pip install SpotiFLAC[dedup] (also needs the 'fpcalc' "
                "binary from Chromaprint on PATH)"
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
        if not path:
            return {"status": "error", "error": "No path given"}
        path = path.strip().strip("'\"")
        import os

        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return {"status": "error", "error": f"Path does not exist: {path}"}

        # Path traversal protection — same approved-roots check scan_local() uses.
        from pathlib import Path

        try:
            resolved = Path(path).resolve()
            approved_roots = [Path(self.download_dir).resolve(), Path.home().resolve()]
            if not any(_is_within(resolved, root) for root in approved_roots):
                return {
                    "status": "error",
                    "error": "Access denied: path is outside approved directories",
                }
        except Exception as e:
            return {"status": "error", "error": f"Path validation failed: {e}"}

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
                compute_fingerprint,
                find_duplicate_groups,
                is_available,
            )
            from ..core.local_scanner import SUPPORTED_EXTENSIONS

            if not is_available():
                self._push(
                    "app_dedup_error",
                    "Duplicate detection requires the optional 'pyacoustid' "
                    "package and the 'fpcalc' binary — see get_dedup_status().",
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


def _is_within(path, root) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

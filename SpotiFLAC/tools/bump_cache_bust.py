"""Bump the frontend cache-bust query string (?v=YYYYMMDD) across every file
that carries it, so a static asset (styles.css, app.js, toast-system.js,
manifest.json) can't be served stale from a browser/webview cache after a
frontend change.

Usage:
    python -m SpotiFLAC.tools.bump_cache_bust            # stamp today's date
    python -m SpotiFLAC.tools.bump_cache_bust 20260915    # explicit value

Touches, in place:
    SpotiFLAC/frontend/index.html   (manifest.json, styles.css, toast-system.js, app.js)
    SpotiFLAC/webapp.py             (web-shim.js, toast-system.js injected tags)

Does not touch sw.js's CACHE_NAME (that's a separate, coarser cache
generation and bumping it on every static tweak would force a full
service-worker re-install more often than needed) — bump it by hand when a
change actually needs clients' offline cache invalidated.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "frontend" / "index.html", ROOT / "webapp.py"]

VERSION_RE = re.compile(r"(\?v=)\d{8}")


def bump(new_version: str) -> None:
    if not re.fullmatch(r"\d{8}", new_version):
        raise SystemExit(f"version must be YYYYMMDD, got {new_version!r}")

    total = 0
    for path in TARGETS:
        if not path.exists():
            print(f"skip (missing): {path}")
            continue
        text = path.read_text(encoding="utf-8")
        new_text, count = VERSION_RE.subn(rf"\g<1>{new_version}", text)
        if count:
            path.write_text(new_text, encoding="utf-8")
        total += count
        print(
            f"{path.relative_to(ROOT.parent)}: {count} occurrence(s) -> ?v={new_version}"
        )

    if total == 0:
        print("Nothing matched — is the ?v=YYYYMMDD pattern still present?")


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
    bump(version)

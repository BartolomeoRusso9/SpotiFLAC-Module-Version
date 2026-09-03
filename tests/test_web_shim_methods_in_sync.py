"""Regression guard: web-shim.js's REMOTE_METHODS allowlist and webapp.py's
ALLOWED_METHODS must list the exact same method names.

They're two independent lists (one JS, one Python) that have to agree for
a method to actually be callable in --web mode: webapp.py's dynamic
dispatcher only serves what's in ALLOWED_METHODS, and web-shim.js only
exposes `window.pywebview.api.<name>` for what's in REMOTE_METHODS — a
method present in only one of the two is either unreachable from the
frontend (Python-only) or a 404 waiting to happen (JS-only). This test
exists because exactly that drift happened once already: several new
mixin methods were added to ALLOWED_METHODS without the matching
web-shim.js update.
"""

from __future__ import annotations

import re
from pathlib import Path

from SpotiFLAC.webapp import ALLOWED_METHODS

_WEB_SHIM = (
    Path(__file__).resolve().parents[1] / "SpotiFLAC" / "frontend" / "web-shim.js"
)


def _remote_methods_from_web_shim() -> set[str]:
    text = _WEB_SHIM.read_text(encoding="utf-8")
    match = re.search(r"REMOTE_METHODS\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert match, "Could not find REMOTE_METHODS array in web-shim.js"
    return set(re.findall(r"'([a-zA-Z_][a-zA-Z0-9_]*)'", match.group(1)))


def test_web_shim_and_allowed_methods_match_exactly() -> None:
    shim_methods = _remote_methods_from_web_shim()

    missing_from_shim = ALLOWED_METHODS - shim_methods
    assert not missing_from_shim, (
        f"Methods in webapp.py's ALLOWED_METHODS but missing from "
        f"web-shim.js's REMOTE_METHODS (unreachable in --web mode): "
        f"{sorted(missing_from_shim)}"
    )

    extra_in_shim = shim_methods - ALLOWED_METHODS
    assert not extra_in_shim, (
        f"Methods in web-shim.js's REMOTE_METHODS but missing from "
        f"webapp.py's ALLOWED_METHODS (would 404 if called): "
        f"{sorted(extra_in_shim)}"
    )


def _pushed_event_names() -> set[str]:
    """Every `self._push("name", …)` in the Python source."""
    package = Path(__file__).resolve().parents[1] / "SpotiFLAC"
    names: set[str] = set()
    for source in package.rglob("*.py"):
        for match in re.finditer(
            r'_push\(\s*"([a-zA-Z_][a-zA-Z0-9_]*)"', source.read_text(encoding="utf-8")
        ):
            names.add(match.group(1))
    return names


def _allowed_push_fns_from_web_shim() -> set[str]:
    text = _WEB_SHIM.read_text(encoding="utf-8")
    match = re.search(r"ALLOWED_PUSH_FNS = new Set\(\[(.*?)\]\)", text, re.DOTALL)
    assert match, "Could not find ALLOWED_PUSH_FNS in web-shim.js"
    return set(re.findall(r"'([a-zA-Z_][a-zA-Z0-9_]*)'", match.group(1)))


def test_every_pushed_event_is_dispatched_by_the_web_shim() -> None:
    """The other half of the same drift, and the one that fails silently.

    A method missing from REMOTE_METHODS 404s loudly. A *push* name missing
    from ALLOWED_PUSH_FNS is dropped with nothing but a console warning: the
    backend runs the whole scan, sends the result, and the UI never hears
    about it. Three names had already drifted out this way — the fingerprint
    duplicate finder's two among them — before this test existed.
    """
    missing = _pushed_event_names() - _allowed_push_fns_from_web_shim()
    assert not missing, (
        "Events pushed by the backend but not dispatched by web-shim.js "
        f"(silently dropped in --web mode): {sorted(missing)}"
    )

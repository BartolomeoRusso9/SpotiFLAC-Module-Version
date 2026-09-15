"""SpotiFLAC/core/signed_session_status.py — which signed sessions do I have?

Three stores live in ``~/.spotiflac/signed_sessions/``, written by three
different modules:

* ``<namespace>-<hash>.json`` — gateway sessions (``signed_session_mobile``),
  one per extension that declares ``signedSession`` in its manifest;
* ``community_sessions.json`` — the Community signed-desktop session
  (``signed_session_desktop``), shared by the Python providers;
* ``monochrome_sessions.json`` — the Monochrome JWT (``signed_session_mono``).

None of them says which extension it belongs to. The gateway file name does,
indirectly: the hash is ``sha256(namespace, baseUrl, appVersion, platform)``
(see ``SignedSessionClient._session_path``), so recomputing it from each
installed manifest joins file to extension. A file no installed manifest
hashes to is *orphaned* — usually left behind by an extension update, since
``appVersion`` is part of the hash and a new version starts a new session.

This module only reads those files, and never returns a secret. The two
writes it offers (clearing one session, removing orphaned ones) are the same
edits the owning modules make themselves: ``install_id`` is always kept.

It deliberately does not import the three session modules: two of them pull
in a browser automation stack, and a status listing has no business starting
that.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import data_path

COMMUNITY_FILE = "community_sessions.json"
MONOCHROME_FILE = "monochrome_sessions.json"

#: Source markers that tell which extensions use the two shared stores.
_SHARED_MARKERS = {
    "community": "signed_session_desktop",
    "monochrome": "signed_session_mono",
}

#: States, best first. The frontends colour by these.
ACTIVE = "active"
REFRESH_DUE = "refresh_due"
EXPIRED = "expired"
UNVERIFIED = "unverified"
ORPHANED = "orphaned"


def sessions_dir() -> Path:
    return data_path("signed_sessions")


def extensions_dir() -> Path:
    return data_path("extensions")


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, record: dict) -> None:
    """Owner-only, atomic — the same way the session modules write these."""
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(record, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def gateway_file_stem(block: dict) -> str | None:
    """The ``<namespace>-<hash>`` a manifest's ``signedSession`` block writes to."""
    try:
        namespace = str(block["namespace"])
        base_url = str(block["baseUrl"]).rstrip("/")
    except (KeyError, TypeError):
        return None
    app_version = str(block.get("appVersion", "1.0"))
    platform = str(block.get("platform", "extension"))
    scope = f"{namespace}\n{base_url.lower()}\n{app_version.lower()}\n{platform.lower()}"
    return f"{namespace}-{hashlib.sha256(scope.encode()).hexdigest()[:16]}"


def _scan_extensions(ext_dir: Path) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """({file stem: {extension, version, base_url, namespace}}, {kind: [ext]})."""
    gateway: dict[str, dict] = {}
    shared: dict[str, list[str]] = {kind: [] for kind in _SHARED_MARKERS}
    try:
        entries = sorted(ext_dir.iterdir())
    except OSError:
        return gateway, shared

    for entry in entries:
        if entry.name.startswith(".") or entry.name.endswith(".previous"):
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_json(manifest_path)
        name = str(manifest.get("name") or entry.name)
        block = manifest.get("signedSession")
        if isinstance(block, dict):
            stem = gateway_file_stem(block)
            if stem:
                gateway[stem] = {
                    "extension": name,
                    "version": str(manifest.get("version") or ""),
                    "base_url": str(block.get("baseUrl", "")).rstrip("/"),
                    "namespace": str(block.get("namespace", "")),
                }
        for source in list(entry.glob("*.py")) + list(entry.glob("*.js")):
            try:
                text = source.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for kind, marker in _SHARED_MARKERS.items():
                if marker in text and name not in shared[kind]:
                    shared[kind].append(name)
    return gateway, shared


def _auth_paused_s(directory: Path, info: dict, now_ts: float) -> float:
    """Seconds left on the gateway's authentication backoff, or 0.

    Mirrors ``signed_session_mobile._auth_backoff_path``: one file per
    namespace and gateway, shared by every extension on it.
    """
    if not info.get("base_url") or not info.get("namespace"):
        return 0.0
    gateway = hashlib.sha256(info["base_url"].lower().encode()).hexdigest()[:12]
    data = _read_json(directory / f".{info['namespace']}-{gateway}.auth-backoff.json")
    try:
        return max(0.0, float(data.get("not_before") or 0) - now_ts)
    except (TypeError, ValueError):
        return 0.0


def _state(has_credentials: bool, expires: datetime | None, refresh: datetime | None,
           now: datetime) -> str:
    if not has_credentials:
        return UNVERIFIED
    if expires is not None and now >= expires:
        return EXPIRED
    if refresh is not None and now >= refresh:
        return REFRESH_DUE
    return ACTIVE


def _seconds_until(moment: datetime | None, now: datetime) -> float | None:
    return None if moment is None else (moment - now).total_seconds()


def list_signed_sessions(
    directory: Path | None = None,
    ext_dir: Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """One row per session file, gateway sessions first, then the shared ones.

    Row keys: ``key`` (the file stem, what :func:`clear_signed_session`
    takes), ``kind`` (gateway / community / monochrome), ``label``,
    ``extensions``, ``version``, ``state``, ``expires_at``,
    ``expires_in_s``, ``refresh_after``, ``refresh_in_s``, ``capabilities``,
    ``auth_paused_s``. Never a secret.
    """
    directory = directory or sessions_dir()
    ext_dir = ext_dir or extensions_dir()
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    if not directory.is_dir():
        return []

    gateway_index, shared = _scan_extensions(ext_dir)
    rows: list[dict] = []

    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue
        record = _read_json(path)
        stem = path.stem
        expires = _parse_time(record.get("expires_at"))

        if path.name in (COMMUNITY_FILE, MONOCHROME_FILE):
            kind = "community" if path.name == COMMUNITY_FILE else "monochrome"
            has_credentials = (
                bool(record.get("session_id") and record.get("session_secret"))
                if kind == "community"
                else bool(record.get("jwt"))
            )
            rows.append(
                {
                    "key": stem,
                    "kind": kind,
                    "label": "Community (signed-desktop)"
                    if kind == "community"
                    else "Monochrome",
                    "extensions": list(shared[kind]),
                    "version": "",
                    "state": _state(has_credentials, expires, None, now),
                    "expires_at": record.get("expires_at") or None,
                    "expires_in_s": _seconds_until(expires, now)
                    if has_credentials
                    else None,
                    "refresh_after": None,
                    "refresh_in_s": None,
                    "capabilities": [],
                    "auth_paused_s": 0.0,
                }
            )
            continue

        if "install_id" not in record and "session_id" not in record:
            continue  # not a session store

        info = gateway_index.get(stem)
        refresh = _parse_time(record.get("refresh_after"))
        has_credentials = bool(record.get("session_id") and record.get("session_secret"))
        state = _state(has_credentials, expires, refresh, now)
        if info is None:
            state = ORPHANED
        capabilities = record.get("capabilities")
        rows.append(
            {
                "key": stem,
                "kind": "gateway",
                "label": info["extension"] if info else stem,
                "extensions": [info["extension"]] if info else [],
                "version": info["version"] if info else "",
                "state": state,
                "expires_at": record.get("expires_at") or None,
                "expires_in_s": _seconds_until(expires, now) if has_credentials else None,
                "refresh_after": record.get("refresh_after") or None,
                "refresh_in_s": _seconds_until(refresh, now) if has_credentials else None,
                "capabilities": [str(c) for c in capabilities]
                if isinstance(capabilities, list)
                else [],
                "auth_paused_s": _auth_paused_s(directory, info, now_ts) if info else 0.0,
            }
        )

    order = {"gateway": 0, "community": 1, "monochrome": 2}
    rows.sort(key=lambda r: (order[r["kind"]], r["state"] == ORPHANED, r["label"]))
    return rows


def _session_file(key: str, directory: Path) -> Path | None:
    """The file for ``key``, only if it is one this module would list."""
    if not key or "/" in key or "\\" in key or key.startswith("."):
        return None
    path = directory / f"{key}.json"
    return path if path.is_file() else None


def clear_signed_session(key: str, directory: Path | None = None) -> bool:
    """Drops one session's credentials, so the next request verifies again.

    ``install_id`` stays: it identifies this install to the gateway, and a
    new one would look like a new device. Returns False for an unknown key.
    """
    directory = directory or sessions_dir()
    path = _session_file(key, directory)
    if path is None:
        return False
    record = _read_json(path)
    if path.name == MONOCHROME_FILE:
        record.update({"jwt": "", "expires_at": ""})
    elif path.name == COMMUNITY_FILE:
        record.update({"session_id": "", "session_secret": "", "expires_at": ""})
    else:
        record.update(
            {
                "session_id": None,
                "session_secret": None,
                "expires_at": None,
                "refresh_after": None,
                "capabilities": [],
            }
        )
    _write_json(path, record)
    return True


def prune_orphaned_sessions(
    directory: Path | None = None, ext_dir: Path | None = None
) -> list[str]:
    """Deletes gateway session files no installed extension uses. Returns keys."""
    directory = directory or sessions_dir()
    removed: list[str] = []
    for row in list_signed_sessions(directory, ext_dir):
        if row["state"] != ORPHANED:
            continue
        path = _session_file(row["key"], directory)
        if path is None:
            continue
        with contextlib.suppress(OSError):
            path.unlink()
            removed.append(row["key"])
    return removed


def format_duration(seconds: float | None) -> str:
    """``2h 05m`` / ``12m`` / ``40s``; ``—`` when unknown."""
    if seconds is None:
        return "—"
    seconds = abs(int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


STATE_LABELS = {
    ACTIVE: "active",
    REFRESH_DUE: "refresh due",
    EXPIRED: "expired",
    UNVERIFIED: "not verified",
    ORPHANED: "orphaned",
}


def describe_expiry(row: dict) -> str:
    """``in 1h 12m`` / ``2h 05m ago`` / ``—`` — the human half of a row."""
    seconds = row.get("expires_in_s")
    if seconds is None:
        return "—"
    return f"in {format_duration(seconds)}" if seconds > 0 else f"{format_duration(seconds)} ago"


def describe_row_detail(row: dict) -> str:
    parts: list[str] = []
    if row.get("state") == ACTIVE and row.get("refresh_in_s") is not None:
        parts.append(f"refresh in {format_duration(row['refresh_in_s'])}")
    if row.get("auth_paused_s"):
        parts.append(f"verification paused {format_duration(row['auth_paused_s'])}")
    if row.get("kind") != "gateway" and row.get("extensions"):
        parts.append("used by " + ", ".join(row["extensions"]))
    if row.get("state") == ORPHANED:
        parts.append("no installed extension uses it")
    return " · ".join(parts)


def print_signed_sessions_report(rows: list[dict]) -> None:
    if not rows:
        print("No signed sessions stored yet.")
        return
    headers = ("Session", "Key", "State", "Expires", "Detail")
    table = [
        (
            row["label"] + (f" v{row['version']}" if row.get("version") else ""),
            row["key"],
            STATE_LABELS.get(row["state"], row["state"]),
            describe_expiry(row),
            describe_row_detail(row),
        )
        for row in rows
    ]
    widths = [max(len(h), *(len(r[i]) for r in table)) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("─" * w for w in widths))
    for r in table:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    orphaned = sum(1 for row in rows if row["state"] == ORPHANED)
    if orphaned:
        print(
            f"\n{orphaned} orphaned session file(s) — remove them with "
            "--signed-sessions-prune."
        )

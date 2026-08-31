"""Crash-safe JSON writes for the small state files under ~/.spotiflac
(including its .cache subdirectory).

`Path.write_text()` truncates the destination before it writes: interrupt it
— a crash, a full disk, a power cut, two threads at once — and what's left on
disk is a truncated or empty file, not the previous contents. For the files
this module exists for that isn't a cosmetic loss: web_users.json holds the
only copy of every account, and trusted_keys.json holds the root of trust for
extension signatures.

The write-temp-then-rename pattern below is the same one response_cache.py
already used; this just makes it available to the other callers instead of
each one open-coding it (or not).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

_PRIVATE_DIR_MODE = 0o700
_PUBLIC_FILE_MODE = 0o644


def write_json_atomic(path: Path, data: Any, *, private: bool = False) -> None:
    """Writes `data` as JSON to `path`, atomically.

    Either the new content is fully in place or the old file is untouched;
    there is no window in which the file exists but is incomplete.

    `private=True` additionally restricts the file to the owner (and the
    directory it lives in), for anything holding credentials or trust
    material. On Windows the chmod call is a no-op — the ACL model doesn't
    map onto POSIX mode bits — so treat it as best-effort there.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        try:
            os.chmod(path.parent, _PRIVATE_DIR_MODE)
        except OSError:
            pass

    # Same directory as the destination: os.replace() is only atomic within a
    # single filesystem, and /tmp is routinely a different one.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        # mkstemp already creates the file 0600, and os.replace() carries the
        # temp file's mode over to the destination — so a private file needs
        # nothing further, while a public one has to be widened back out to
        # what an ordinary write would have produced.
        if not private and hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, _PUBLIC_FILE_MODE)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            # Without the fsync the rename can land before the bytes do, which
            # on a hard power loss leaves a correctly-named empty file — the
            # exact outcome the temp-file dance is meant to prevent.
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Persists the rename itself, not just the file's contents.

    fsync on the file guarantees the bytes are on disk; it says nothing
    about the *directory entry* that now points at them. On a hard power
    loss the rename can still be lost, leaving the old file — or, worse, a
    name pointing at nothing. Directory fsync is POSIX-only; Windows has no
    equivalent and raises, which is why this is best-effort.
    """
    if os.name == "nt":
        return
    fd = None
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def write_private_json(path: Path, data: Any) -> None:
    """write_json_atomic(..., private=True) — for credential/trust files."""
    write_json_atomic(path, data, private=True)

"""csv_picker.py — Finding the track list someone meant.

A CSV is the other kind of input SpotiFLAC takes: a list of tracks instead of
a link to one. Naming the file is the awkward part — the path is long, it was
exported five minutes ago into whichever folder the browser uses, and typing
it out is the step people get wrong.

So this module answers two questions and leaves the asking to whoever owns
the screen: *what did they actually type* (`clean_path_input`, which has to
survive a path dragged in from a shell, quotes and escapes and all), and
*what is lying around that looks like a track list* (`scan_csv_files` over
`csv_scan_dirs`).

None of it prints. It started inside the interactive wizard, where it did,
and moved here so the terminal UI could offer the same picker without
inheriting a wizard's idea of how to draw one.
"""

from __future__ import annotations

import asyncio
import os
import shlex

#: What a track list is called.
CSV_SUFFIXES: tuple[str, ...] = (".csv", ".tsv")

#: How many candidates a picker should offer. Past this the list stops being
#: a shortcut and becomes something to read.
CSV_SCAN_LIMIT = 15

#: Typed where a URL goes, these mean "let me look for the file instead".
CSV_BROWSE_WORDS = frozenset({"csv", "tsv", "file", "browse", "pick"})

def clean_path_input(value: str) -> str:
    """Turn what a terminal hands us into a path that can be opened.

    A file dragged into the terminal arrives quoted, or — on a Unix shell —
    with its spaces backslash-escaped (``/Users/me/My\\ tracks.csv``). Both
    are undone here, and `~` is expanded so a typed home path works too.

    The unescaping is POSIX-only on purpose: on Windows the backslash is the
    path separator, and running ``C:\\Users\\me\\list.csv`` through `shlex`
    hands back ``C:Usersmelist.csv``. Quotes are stripped on both, since
    that is what dragging a path with a space into either shell produces.
    """
    raw = (value or "").strip()
    if not raw:
        return ""

    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        candidate = raw[1:-1]
    elif os.name == "nt":
        candidate = raw
    else:
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = []
        # More than one part means the spaces were never escaped, so the
        # whole line is the path — splitting it would only lose the rest.
        candidate = parts[0] if len(parts) == 1 else raw

    return os.path.expanduser(candidate)


def looks_like_csv_path(value: str) -> bool:
    return value.lower().endswith(CSV_SUFFIXES)


def short_dir(path: str) -> str:
    """`/Users/me/Downloads` reads better as `~/Downloads` in a menu."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def csv_scan_dirs(extra: str = "", last_folder: str = "") -> list[str]:
    """Where a track list is likely to be, most specific first.

    `extra` is a folder the user just named, so it wins over the guesses.
    """
    home = os.path.expanduser("~")
    raw = [
        extra,
        os.getcwd(),
        os.path.join(os.getcwd(), "Downloads"),
        last_folder,
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Music"),
    ]

    dirs: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not entry:
            continue
        path = os.path.abspath(os.path.expanduser(entry))
        if path in seen or not os.path.isdir(path):
            continue
        seen.add(path)
        dirs.append(path)
    return dirs


def scan_csv_files(dirs: list[str], limit: int = CSV_SCAN_LIMIT) -> list[tuple]:
    """The CSV/TSV files in `dirs`, as (path, mtime, size).

    Ordered by folder first and modification time second, so a folder the
    user just named stays at the top of the list instead of being scattered
    through whatever else is newer somewhere else.

    One level deep only: scanning a home folder recursively is slow enough to
    be noticed, and the file someone means is nearly always the one they just
    exported into a folder they can name.
    """
    found: list[tuple] = []
    seen: set[str] = set()
    for rank, directory in enumerate(dirs):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not looks_like_csv_path(entry.name):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            real = os.path.realpath(entry.path)
            if real in seen:
                continue
            seen.add(real)
            found.append((rank, entry.path, stat.st_mtime, stat.st_size))

    found.sort(key=lambda item: (item[0], -item[2]))
    return [(path, mtime, size) for _rank, path, mtime, size in found[:limit]]


async def read_csv_document(path: str):
    """Parse `path`, returning (document, error message). Never raises."""
    try:
        from . import csv_source
    except Exception as exc:
        return None, f"CSV support is unavailable: {exc}"
    try:
        document = await asyncio.to_thread(csv_source.read_rows, path)
    except Exception as exc:
        return None, str(exc) or exc.__class__.__name__
    return document, ""

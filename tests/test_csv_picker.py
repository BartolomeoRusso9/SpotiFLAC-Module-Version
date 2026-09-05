"""Finding the track list someone meant.

The path handling and the folder scan, which are the half that actually
decides whether the right file is found. They are pure, they live in
`core/csv_picker.py`, and both the terminal UI and anything else that wants
to offer a picker read them from there.

The UI on top of them is tested separately, against the screen that draws it:
`tests/test_tui_csv_picker.py`.
"""

from __future__ import annotations

import os

import pytest

from SpotiFLAC.core import csv_picker

_EXPORT = (
    "Track Name,Artist Name(s),Album Name,ISRC\n"
    "Blinding Lights,The Weeknd,After Hours,USUG11904206\n"
    "Dreams,Fleetwood Mac,Rumours,USEE10001993\n"
)


@pytest.fixture
def csv_dir(tmp_path):
    (tmp_path / "export.csv").write_text(_EXPORT, encoding="utf-8")
    (tmp_path / "list.tsv").write_text(
        "title\tartist\nBad Guy\tBillie Eilish\n", encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("not a track list", encoding="utf-8")
    # Fixed times: the menu is ordered by recency, and same-second writes
    # would otherwise make "entry 1" depend on the filesystem's clock.
    os.utime(tmp_path / "export.csv", (2_000, 2_000))
    os.utime(tmp_path / "list.tsv", (1_000, 1_000))
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path_factory):
    """Keep the picker inside the test's own folders.

    Its default search list is the working directory plus the usual home
    folders, so without this the assertions would depend on whatever CSVs
    the machine running the tests happens to have in ~/Downloads.
    """
    home = tmp_path_factory.mktemp("home")
    workdir = tmp_path_factory.mktemp("cwd")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(workdir)


def test_clean_path_input_undoes_terminal_escaping(monkeypatch):
    """A Unix shell escapes the spaces in a dragged path.

    Pinned to posix: the same backslashes are separators on Windows, and the
    two behaviours are opposites — see the Windows test below.
    """
    monkeypatch.setattr(os, "name", "posix")
    assert csv_picker.clean_path_input("/tmp/My\\ tracks.csv") == "/tmp/My tracks.csv"


def test_clean_path_input_strips_quotes_anywhere(monkeypatch):
    """Quoting is what both shells do with a path that has a space in it."""
    for name in ("posix", "nt"):
        monkeypatch.setattr(os, "name", name)
        assert csv_picker.clean_path_input("'/tmp/a b.csv'") == "/tmp/a b.csv"
        assert csv_picker.clean_path_input('  "/tmp/x.csv" ') == "/tmp/x.csv"
        assert csv_picker.clean_path_input("") == ""


def test_clean_path_input_keeps_unquoted_spaces(monkeypatch):
    """Neither branch may drop half the line when nothing was escaped."""
    for name in ("posix", "nt"):
        monkeypatch.setattr(os, "name", name)
        assert (
            csv_picker.clean_path_input("/tmp/My tracks.csv") == "/tmp/My tracks.csv"
        )


def test_clean_path_input_leaves_windows_separators_alone(monkeypatch):
    """On Windows the backslash is the path separator, not an escape.

    Unescaping there turned `C:\\Users\\me\\list.csv` into
    `C:Usersmelist.csv`, and every pasted path was answered with
    "No such file".
    """
    monkeypatch.setattr(os, "name", "nt")
    assert csv_picker.clean_path_input(r"C:\Users\me\list.csv") == (
        r"C:\Users\me\list.csv"
    )
    # Dragging a path with a space into a Windows shell quotes it instead.
    assert csv_picker.clean_path_input(r'"C:\Users\me\my list.csv"') == (
        r"C:\Users\me\my list.csv"
    )


def test_looks_like_csv_path_is_case_insensitive():
    assert csv_picker.looks_like_csv_path("a.CSV")
    assert csv_picker.looks_like_csv_path("a.tsv")
    assert not csv_picker.looks_like_csv_path("a.txt")


def test_scan_lists_only_track_lists(csv_dir):
    found = csv_picker.scan_csv_files([str(csv_dir)])
    names = sorted(os.path.basename(path) for path, _mtime, _size in found)
    assert names == ["export.csv", "list.tsv"]


def test_scan_orders_by_folder_then_recency(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "old.csv").write_text(_EXPORT, encoding="utf-8")
    (second / "new.csv").write_text(_EXPORT, encoding="utf-8")
    os.utime(first / "old.csv", (1_000, 1_000))

    found = csv_picker.scan_csv_files([str(first), str(second)])
    assert [os.path.basename(path) for path, _m, _s in found] == ["old.csv", "new.csv"]


def test_scan_skips_unreadable_folders(tmp_path):
    assert csv_picker.scan_csv_files([str(tmp_path / "missing")]) == []


def test_scan_respects_the_limit(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.csv").write_text(_EXPORT, encoding="utf-8")
    assert len(csv_picker.scan_csv_files([str(tmp_path)], limit=3)) == 3


def test_scan_dirs_puts_the_named_folder_first(csv_dir):
    dirs = csv_picker.csv_scan_dirs(str(csv_dir))
    assert dirs[0] == str(csv_dir)


def test_scan_dirs_drops_paths_that_are_not_folders(tmp_path):
    assert str(tmp_path / "nope") not in csv_picker.csv_scan_dirs(
        str(tmp_path / "nope")
    )


def test_browse_words_cover_what_the_prompt_advertises():
    assert "csv" in csv_picker.CSV_BROWSE_WORDS


def test_short_dir_uses_a_tilde(monkeypatch, tmp_path):
    # expanduser() reads HOME on Unix and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert csv_picker.short_dir(str(tmp_path)) == "~"
    assert csv_picker.short_dir(str(tmp_path / "Downloads")) == os.path.join(
        "~", "Downloads"
    )
    assert csv_picker.short_dir("/etc") == "/etc"


def test_format_size_reads_as_a_file_manager_would():
    assert csv_picker.format_size(512) == "512 B"
    assert csv_picker.format_size(2048) == "2.0 KB"
    assert csv_picker.format_size(5 * 1024 * 1024) == "5.0 MB"

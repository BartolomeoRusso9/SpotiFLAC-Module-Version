"""Tests for interactive.py's CSV picker — the helper that lets the wizard
browse for a track list instead of making someone type its path by hand.

The picker's input() loop is driven with a fed sequence of answers; the
scanning and formatting helpers around it are pure and tested directly.
"""

from __future__ import annotations

import asyncio
import builtins
import os

import pytest

from SpotiFLAC import interactive

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


@pytest.fixture
def feed(monkeypatch):
    """Answer the picker's prompts with a fixed sequence."""

    def _feed(*answers: str) -> None:
        pending = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda _prompt="": next(pending))

    return _feed


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
    monkeypatch.setattr(
        interactive, "_last_output_folder", lambda: asyncio.sleep(0, result="")
    )


def test_clean_path_input_undoes_terminal_escaping():
    assert interactive._clean_path_input("/tmp/My\\ tracks.csv") == "/tmp/My tracks.csv"
    assert interactive._clean_path_input("'/tmp/a b.csv'") == "/tmp/a b.csv"
    assert interactive._clean_path_input('  "/tmp/x.csv" ') == "/tmp/x.csv"
    assert interactive._clean_path_input("") == ""


def test_clean_path_input_keeps_unquoted_spaces(tmp_path):
    # shlex splits this into two words; the raw path is the useful reading.
    assert interactive._clean_path_input("/tmp/My tracks.csv") == "/tmp/My tracks.csv"


def test_looks_like_csv_path_is_case_insensitive():
    assert interactive._looks_like_csv_path("a.CSV")
    assert interactive._looks_like_csv_path("a.tsv")
    assert not interactive._looks_like_csv_path("a.txt")


def test_scan_lists_only_track_lists(csv_dir):
    found = interactive._scan_csv_files([str(csv_dir)])
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

    found = interactive._scan_csv_files([str(first), str(second)])
    assert [os.path.basename(path) for path, _m, _s in found] == ["old.csv", "new.csv"]


def test_scan_skips_unreadable_folders(tmp_path):
    assert interactive._scan_csv_files([str(tmp_path / "missing")]) == []


def test_scan_respects_the_limit(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.csv").write_text(_EXPORT, encoding="utf-8")
    assert len(interactive._scan_csv_files([str(tmp_path)], limit=3)) == 3


def test_scan_dirs_puts_the_named_folder_first(csv_dir):
    dirs = interactive._csv_scan_dirs(str(csv_dir))
    assert dirs[0] == str(csv_dir)


def test_scan_dirs_drops_paths_that_are_not_folders(tmp_path):
    assert str(tmp_path / "nope") not in interactive._csv_scan_dirs(
        str(tmp_path / "nope")
    )


def test_pick_by_number_returns_a_validated_path(csv_dir, feed, capsys):
    feed("1")
    picked = asyncio.run(interactive._pick_csv_file(str(csv_dir)))
    assert picked == str(csv_dir / "export.csv")
    out = capsys.readouterr().out
    # The preview is what tells someone they chose last month's export.
    assert "2 tracks" in out
    assert "Blinding Lights" in out


def test_pasted_path_is_accepted(csv_dir, feed):
    feed(f"'{csv_dir / 'list.tsv'}'")
    assert asyncio.run(interactive._pick_csv_file()) == str(csv_dir / "list.tsv")


def test_a_folder_rescans_instead_of_failing(csv_dir, feed, capsys):
    feed(str(csv_dir), "1")
    assert asyncio.run(interactive._pick_csv_file()) == str(csv_dir / "export.csv")


def test_missing_file_asks_again_rather_than_returning_it(csv_dir, feed, capsys):
    feed(str(csv_dir / "nope.csv"), "")
    assert asyncio.run(interactive._pick_csv_file(str(csv_dir))) is None
    assert "No such file" in capsys.readouterr().out


def test_unparsable_file_is_rejected(tmp_path, feed, capsys):
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    feed(str(empty), "")
    assert asyncio.run(interactive._pick_csv_file(str(tmp_path))) is None
    assert "empty.csv" in capsys.readouterr().out


def test_empty_answer_cancels(csv_dir, feed):
    feed("")
    assert asyncio.run(interactive._pick_csv_file(str(csv_dir))) is None


def test_back_restarts_the_wizard(csv_dir, feed):
    feed("b")
    with pytest.raises(interactive._BackRequested):
        asyncio.run(interactive._pick_csv_file(str(csv_dir)))


def test_browse_words_cover_what_the_prompt_advertises():
    assert "csv" in interactive._CSV_BROWSE_WORDS


def test_short_dir_uses_a_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert interactive._short_dir(str(tmp_path)) == "~"
    assert interactive._short_dir(str(tmp_path / "Downloads")) == os.path.join(
        "~", "Downloads"
    )
    assert interactive._short_dir("/etc") == "/etc"


def test_format_size_reads_as_a_file_manager_would():
    assert interactive._format_size(512) == "512 B"
    assert interactive._format_size(2048) == "2.0 KB"
    assert interactive._format_size(5 * 1024 * 1024) == "5.0 MB"

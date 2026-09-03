"""Tests for core/library_dedup.py — finding and resolving duplicates.

The grouping and ranking logic is exercised directly on LibraryFile values,
which needs no files at all. The scan and the resolution are exercised on
real audio built with ffmpeg where it is available, because what they are
actually being trusted with is moving and deleting files.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from SpotiFLAC.core import library_dedup as ld
from SpotiFLAC.core.library_dedup import (
    ACTION_DELETE,
    LibraryFile,
    group_duplicates,
    normalize_artist,
    normalize_title,
    rank_key,
    resolve_duplicates,
    restore_manifest,
    scan_duplicates,
)

ffmpeg_required = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs ffmpeg to build audio fixtures"
)


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path_factory):
    """The scan cache must not land in the developer's ~/.spotiflac."""
    monkeypatch.setenv(
        "SPOTIFLAC_CACHE_DIR", str(tmp_path_factory.mktemp("dedup-cache"))
    )


def _make_audio(path: Path, *, codec: str, duration: float = 2.0, **tags: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: list[str] = []
    for key, value in tags.items():
        metadata += ["-metadata", f"{key}={value}"]
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}:sample_rate=44100",
            "-c:a",
            codec,
            *metadata,
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _entry(path: str, **kwargs) -> LibraryFile:
    """A LibraryFile that looks plausible without a file behind it."""
    defaults = {
        "size": 1000,
        "mtime_ns": 1,
        "duration_ms": 200_000,
        "tier": 2,
        "lossless": True,
        "sample_rate": 44100,
        "bits_per_sample": 16,
    }
    return LibraryFile(path=path, **{**defaults, **kwargs})


# ── Normalisation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bohemian Rhapsody", "bohemian rhapsody"),
        ("Bohemian Rhapsody (2011 Remaster)", "bohemian rhapsody"),
        ("Bohemian Rhapsody - Remastered 2011", "bohemian rhapsody"),
        ("Bohemian Rhapsody [Remastered]", "bohemian rhapsody"),
        ("Song (Album Version)", "song"),
        ("Song (Remastered) (Album Version)", "song"),
    ],
)
def test_normalize_title_drops_release_noise(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Song (Live)", "Song - Live at Wembley", "Song (Radio Edit)", "Song (Mono)"],
)
def test_normalize_title_keeps_what_changes_the_audio(raw):
    """A live take is a different recording; collapsing it would delete it."""
    assert normalize_title(raw) != normalize_title("Song")


def test_normalize_title_can_keep_the_noise():
    assert normalize_title("Song (Remastered)", strip_version_noise=False) != (
        normalize_title("Song")
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Lazza", "lazza"),
        ("Lazza feat. Sfera Ebbasta", "lazza"),
        ("Lazza, Sfera Ebbasta", "lazza"),
        ("Lazza & Sfera Ebbasta", "lazza"),
        ("Lazza ft Sfera", "lazza"),
        ("Lazza / Sfera", "lazza"),
    ],
)
def test_normalize_artist_keeps_only_the_lead(raw, expected):
    assert normalize_artist(raw) == expected


# ── Grouping ──────────────────────────────────────────────────────────────


def test_isrc_beats_disagreeing_tags():
    entries = [
        _entry("/m/a.flac", isrc="ITB001700001", title="Song", artist="A"),
        _entry("/m/b.mp3", isrc="ITB001700001", title="Totally Different", artist="Z"),
    ]
    groups = group_duplicates(entries)
    assert len(groups) == 1
    assert groups[0].matched_by == "isrc"
    assert {f.path for f in groups[0].files} == {"/m/a.flac", "/m/b.mp3"}


def test_an_untagged_copy_joins_the_group_its_isrc_siblings_formed():
    """The common shape in a real library: one copy carries an ISRC, the
    other was ripped by something that never wrote one. One group, three
    files — and never a file in two groups, which would ask resolution to
    remove it twice."""
    entries = [
        _entry("/m/a.flac", isrc="ITB001700001", title="Song", artist="A"),
        _entry("/m/b.mp3", isrc="ITB001700001", title="Song", artist="A"),
        _entry("/m/c.mp3", title="Song", artist="A"),
    ]
    groups = group_duplicates(entries)
    assert len(groups) == 1
    assert {f.path for f in groups[0].files} == {"/m/a.flac", "/m/b.mp3", "/m/c.mp3"}
    assert groups[0].matched_by == "isrc+tags"
    assert sum(len(g.files) for g in groups) == 3  # no file in two groups


def test_two_different_isrcs_are_never_merged_on_names_alone():
    """Different ISRCs are different recordings; agreeing titles do not
    outrank that, because the cost of being wrong is a deleted file."""
    entries = [
        _entry("/m/a.flac", isrc="ITB001700001", title="Song", artist="A"),
        _entry("/m/b.flac", isrc="USUM71900002", title="Song", artist="A"),
    ]
    assert group_duplicates(entries) == []


def test_an_untagged_copy_between_two_disagreeing_isrcs_joins_neither():
    entries = [
        _entry("/m/a.flac", isrc="ITB001700001", title="Song", artist="A"),
        _entry("/m/b.flac", isrc="USUM71900002", title="Song", artist="A"),
        _entry("/m/c.mp3", title="Song", artist="A"),
        _entry("/m/d.mp3", title="Song", artist="A"),
    ]
    groups = group_duplicates(entries)
    assert len(groups) == 1
    assert {f.path for f in groups[0].files} == {"/m/c.mp3", "/m/d.mp3"}


def test_tags_mode_ignores_an_isrc_disagreement_because_it_was_told_to():
    entries = [
        _entry("/m/a.flac", isrc="ITB001700001", title="Song", artist="A"),
        _entry("/m/b.flac", isrc="USUM71900002", title="Song", artist="A"),
    ]
    assert len(group_duplicates(entries, match="tags")) == 1


def test_tags_group_across_albums_and_formats():
    entries = [
        _entry("/m/album/song.flac", title="Song", artist="A", album="The Album"),
        _entry("/m/singles/song.mp3", title="song", artist="A feat. B", album="Single"),
    ]
    groups = group_duplicates(entries)
    assert len(groups) == 1
    assert groups[0].matched_by == "tags"


def test_duration_keeps_a_different_recording_out_of_the_group():
    entries = [
        _entry("/m/studio.flac", title="Song", artist="A", duration_ms=200_000),
        _entry("/m/live.flac", title="Song", artist="A", duration_ms=420_000),
    ]
    assert group_duplicates(entries) == []


def test_duration_tolerance_is_measured_from_the_first_of_a_cluster():
    """Chaining near-misses must not drag a group past the tolerance."""
    entries = [
        _entry("/m/1.flac", title="S", artist="A", duration_ms=200_000),
        _entry("/m/2.flac", title="S", artist="A", duration_ms=203_000),
        _entry("/m/3.flac", title="S", artist="A", duration_ms=206_000),
    ]
    groups = group_duplicates(entries, duration_tolerance_s=4.0)
    assert len(groups) == 1
    assert {Path(f.path).name for f in groups[0].files} == {"1.flac", "2.flac"}


def test_unknown_duration_still_groups():
    entries = [
        _entry("/m/tagged.flac", title="S", artist="A", duration_ms=200_000),
        _entry("/m/broken.mp3", title="S", artist="A", duration_ms=0),
    ]
    assert len(group_duplicates(entries)) == 1


def test_match_isrc_only_ignores_tag_matches():
    entries = [
        _entry("/m/a.flac", title="S", artist="A"),
        _entry("/m/b.mp3", title="S", artist="A"),
    ]
    assert group_duplicates(entries, match="isrc") == []
    assert len(group_duplicates(entries, match="tags")) == 1


def test_untagged_files_are_never_grouped_by_name():
    entries = [_entry("/m/1.mp3"), _entry("/m/2.mp3")]
    assert group_duplicates(entries) == []


def test_unreadable_files_are_never_grouped():
    entries = [
        _entry("/m/a.flac", title="S", artist="A", error="truncated"),
        _entry("/m/b.flac", title="S", artist="A", error="truncated"),
    ]
    assert group_duplicates(entries) == []


def test_scan_rejects_an_unknown_match_mode(tmp_path):
    with pytest.raises(ValueError, match="match mode"):
        scan_duplicates(tmp_path, match="vibes")


# ── Ranking ───────────────────────────────────────────────────────────────


def test_the_lossless_copy_is_the_one_kept():
    lossy = _entry("/m/a.mp3", tier=1, lossless=False, bitrate=320_000, size=8_000_000)
    lossless = _entry("/m/a.flac", tier=2, size=30_000_000)
    assert sorted([lossy, lossless], key=rank_key)[0] is lossless


def test_between_two_lossless_copies_the_better_master_wins():
    cd = _entry("/m/cd.flac", tier=2, sample_rate=44100, bits_per_sample=16)
    hires = _entry("/m/hr.flac", tier=3, sample_rate=96000, bits_per_sample=24)
    assert sorted([cd, hires], key=rank_key)[0] is hires


def test_metadata_breaks_a_tie_between_identical_files():
    bare = _entry("/m/b.flac", title="S", artist="A")
    tagged = _entry("/m/a.flac", title="S", artist="A", isrc="ITB001700001")
    assert sorted([bare, tagged], key=rank_key)[0] is tagged


def test_ranking_is_deterministic_for_indistinguishable_files():
    a, b = _entry("/m/a.flac"), _entry("/m/b.flac")
    assert sorted([b, a], key=rank_key) == sorted([a, b], key=rank_key)


# ── Scanning real files ───────────────────────────────────────────────────


@ffmpeg_required
def test_scan_finds_the_same_track_in_two_formats(tmp_path):
    _make_audio(tmp_path / "song.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "song.mp3", codec="libmp3lame", title="Song", artist="A")
    _make_audio(tmp_path / "other.flac", codec="flac", title="Other", artist="A")

    report = scan_duplicates(tmp_path)

    assert report.stats.files == 3
    assert len(report.groups) == 1
    assert report.duplicate_files == 1
    assert report.groups[0].keeper.path.endswith(".flac")
    assert report.reclaimable_bytes > 0
    # The recap half of the report, not just the duplicates.
    assert report.stats.by_extension == {"flac": 2, "mp3": 1}
    assert report.stats.missing_isrc == 3
    assert "duplicate_groups" in report.to_dict()


@ffmpeg_required
def test_second_scan_reuses_the_cache(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.flac", codec="flac", title="Song", artist="A")

    assert scan_duplicates(tmp_path).cache_hits == 0
    assert scan_duplicates(tmp_path).cache_hits == 2


@ffmpeg_required
def test_an_edited_file_is_re_read_rather_than_remembered(tmp_path):
    path = _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    scan_duplicates(tmp_path)
    _make_audio(path, codec="flac", title="Renamed", artist="A", duration=3.0)

    report = scan_duplicates(tmp_path)
    assert report.cache_hits == 0


def test_partial_cache_keeps_the_entries_the_walk_never_reached(tmp_path):
    """The bug this cache exists to avoid: a crash mid-walk must not throw
    away everything the previous, complete walk had already learned."""
    cache = ld._ScanCache(tmp_path, checkpoint_every=1)
    cache.put(_entry(str(tmp_path / "a.flac")))
    cache.put(_entry(str(tmp_path / "b.flac")))
    cache.save(complete=True)

    resumed = ld._ScanCache(tmp_path, checkpoint_every=1)
    resumed.put(_entry(str(tmp_path / "a.flac")))  # checkpoints, walk unfinished
    reloaded = ld._ScanCache(tmp_path)
    assert str(tmp_path / "b.flac") in reloaded._entries

    resumed.save(complete=True)  # now the walk is over: b really is gone
    pruned = ld._ScanCache(tmp_path)
    assert str(tmp_path / "b.flac") not in pruned._entries


# ── Resolving ─────────────────────────────────────────────────────────────


@ffmpeg_required
def test_dry_run_reports_without_touching_anything(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    report = scan_duplicates(tmp_path)
    result = resolve_duplicates(report)

    assert result.dry_run
    assert len(result.resolved) == 1
    assert (tmp_path / "a.flac").exists()
    assert (tmp_path / "b.mp3").exists()
    assert not result.manifest_path


@ffmpeg_required
def test_apply_quarantines_and_restore_puts_it_back(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(
        tmp_path / "sub" / "b.mp3", codec="libmp3lame", title="Song", artist="A"
    )

    report = scan_duplicates(tmp_path)
    result = resolve_duplicates(report, dry_run=False)

    assert not (tmp_path / "sub" / "b.mp3").exists()
    assert (tmp_path / "a.flac").exists()
    quarantined = tmp_path / ld.TRASH_DIRNAME / "sub" / "b.mp3"
    assert quarantined.exists()
    assert result.freed_bytes > 0

    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["restorable"] is True
    assert len(manifest["moves"]) == 1

    restored = restore_manifest(result.manifest_path)
    assert (tmp_path / "sub" / "b.mp3").exists()
    assert not quarantined.exists()
    assert len(restored.resolved) == 1


@ffmpeg_required
def test_a_quarantined_copy_is_not_found_again_by_the_next_scan(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    resolve_duplicates(scan_duplicates(tmp_path), dry_run=False)

    assert scan_duplicates(tmp_path).groups == []


@ffmpeg_required
def test_delete_unlinks_and_says_so_in_the_manifest(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    report = scan_duplicates(tmp_path)
    result = resolve_duplicates(report, action=ACTION_DELETE, dry_run=False)

    assert not (tmp_path / "b.mp3").exists()
    assert (tmp_path / "a.flac").exists()
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["restorable"] is False
    with pytest.raises(ValueError, match="nothing to restore"):
        restore_manifest(result.manifest_path)


@ffmpeg_required
def test_a_file_that_changed_since_the_scan_is_left_alone(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    report = scan_duplicates(tmp_path)
    (tmp_path / "b.mp3").write_bytes(b"not the file that was scanned")

    result = resolve_duplicates(report, dry_run=False)
    assert result.resolved == []
    assert "changed since the scan" in result.skipped[0].error
    assert (tmp_path / "b.mp3").exists()


@ffmpeg_required
def test_the_whole_group_is_left_alone_when_the_keeper_moved(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    report = scan_duplicates(tmp_path)
    (tmp_path / "a.flac").unlink()

    result = resolve_duplicates(report, dry_run=False)
    assert result.resolved == []
    assert "gone since the scan" in result.skipped[0].error
    assert (tmp_path / "b.mp3").exists()


@ffmpeg_required
def test_keep_paths_overrides_the_ranking(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    report = scan_duplicates(tmp_path)
    result = resolve_duplicates(
        report, dry_run=False, keep_paths={str(tmp_path / "b.mp3")}
    )

    assert (tmp_path / "b.mp3").exists()
    assert not (tmp_path / "a.flac").exists()
    assert len(result.resolved) == 1


@ffmpeg_required
def test_a_hardlink_to_the_kept_copy_is_not_removed(tmp_path):
    source = _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    (tmp_path / "link.flac").hardlink_to(source)

    report = scan_duplicates(tmp_path)
    result = resolve_duplicates(report, dry_run=False)

    assert (tmp_path / "link.flac").exists()
    assert source.exists()
    assert "link" in result.skipped[0].error


@ffmpeg_required
def test_limit_stops_after_n_files(tmp_path):
    for name in ("a.flac", "b.flac", "c.flac"):
        _make_audio(tmp_path / name, codec="flac", title="Song", artist="A")

    report = scan_duplicates(tmp_path)
    assert report.duplicate_files == 2

    result = resolve_duplicates(report, dry_run=False, limit=1)
    assert len(result.resolved) == 1


def test_resolve_rejects_an_unknown_action(tmp_path):
    report = scan_duplicates(tmp_path)
    with pytest.raises(ValueError, match="Unknown action"):
        resolve_duplicates(report, action="incinerate")


# ── Acoustic verification ─────────────────────────────────────────────────


def _fake_backend(monkeypatch, prints: dict[str, tuple[int, ...]], *, available=True):
    """Stands in for Chromaprint, which is an optional dependency the test
    machine may not have. The comparison itself is the real one.
    """
    from SpotiFLAC.core import audio_fingerprint as af

    def compute(path):
        raw = prints.get(str(path))
        if raw is None:
            msg = f"no fingerprint for {path}"
            raise af.AudioFingerprintError(msg)
        return af.AudioFingerprint(path=Path(path), duration_s=200.0, raw=raw)

    monkeypatch.setattr(af, "can_compare", lambda: available)
    monkeypatch.setattr(af, "compute_fingerprint", compute)


def test_verify_splits_a_group_whose_audio_disagrees(monkeypatch):
    same = tuple([0x0F0F0F0F] * 40)
    other = tuple([0xF0F0F0F0] * 40)
    entries = [
        _entry("/m/a.flac", title="S", artist="A"),
        _entry("/m/b.flac", title="S", artist="A"),
        _entry("/m/c.flac", title="S", artist="A"),
    ]
    groups = group_duplicates(entries)
    assert len(groups[0].files) == 3

    _fake_backend(
        monkeypatch, {"/m/a.flac": same, "/m/b.flac": same, "/m/c.flac": other}
    )
    verified, notes = ld.verify_groups(groups)

    assert notes == []
    assert len(verified) == 1
    assert {f.path for f in verified[0].files} == {"/m/a.flac", "/m/b.flac"}
    assert verified[0].matched_by == "tags+audio"


def test_verification_survives_a_path_that_is_not_its_own_normal_form(monkeypatch):
    """The group is matched back up by the fingerprint's own path object.

    Matching on `str(path)` instead assumed a path string survives a round
    trip through Path unchanged. It does not on Windows — a `/m/a.flac` goes
    in and a `\\m\\a.flac` comes back — so every lookup missed and the
    verified group was dropped as a singleton, quietly confirming nothing.
    `/m/./a.flac` reproduces that on any platform.
    """
    same = tuple([0x0F0F0F0F] * 40)
    entries = [
        _entry("/m/./a.flac", title="S", artist="A"),
        _entry("/m/./b.flac", title="S", artist="A"),
    ]
    groups = group_duplicates(entries)

    _fake_backend(monkeypatch, {"/m/./a.flac": same, "/m/./b.flac": same})
    verified, notes = ld.verify_groups(groups)

    assert notes == []
    assert len(verified) == 1
    assert {f.path for f in verified[0].files} == {"/m/./a.flac", "/m/./b.flac"}


def test_a_file_that_cannot_be_fingerprinted_leaves_the_group(monkeypatch):
    """Unsure has to mean 'keep them all' — everything downstream of a group
    is a decision to remove files."""
    same = tuple([0x0F0F0F0F] * 40)
    entries = [
        _entry("/m/a.flac", title="S", artist="A"),
        _entry("/m/b.flac", title="S", artist="A"),
    ]
    groups = group_duplicates(entries)

    _fake_backend(monkeypatch, {"/m/a.flac": same})
    verified, notes = ld.verify_groups(groups)

    assert verified == []
    assert "could not be fingerprinted" in notes[0]


def test_verification_without_chromaprint_changes_nothing_and_says_so(monkeypatch):
    entries = [
        _entry("/m/a.flac", title="S", artist="A"),
        _entry("/m/b.flac", title="S", artist="A"),
    ]
    groups = group_duplicates(entries)

    _fake_backend(monkeypatch, {}, available=False)
    verified, notes = ld.verify_groups(groups)

    assert verified == groups
    assert "skipped" in notes[0]


def test_a_scan_that_could_not_verify_does_not_claim_it_did(monkeypatch, tmp_path):
    _fake_backend(monkeypatch, {}, available=False)
    report = scan_duplicates(tmp_path, verify=True)
    assert report.verified is False


# ── The scan as a database ────────────────────────────────────────────────


@ffmpeg_required
def test_export_writes_one_row_per_file_not_only_the_duplicates(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")
    _make_audio(tmp_path / "c.flac", codec="flac", title="Other", artist="B")

    report = scan_duplicates(tmp_path)
    db = ld.export_sqlite(report, tmp_path / "index.db")

    assert db.exists()
    assert not (tmp_path / "index.db.partial").exists()

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT count(*) FROM files").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM groups").fetchone()[0] == 1
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        assert meta["schema_version"] == str(ld.DB_SCHEMA_VERSION)
        assert meta["root"] == str(tmp_path)
        assert meta["files"] == "3"

        # The file that is nobody's duplicate is still indexed, with no group.
        ungrouped = connection.execute(
            "SELECT path FROM files WHERE group_id IS NULL"
        ).fetchall()
        assert [Path(row[0]).name for row in ungrouped] == ["c.flac"]

        rows = connection.execute(
            "SELECT role, path FROM duplicates ORDER BY role DESC"
        ).fetchall()
        assert [row[0] for row in rows] == ["keep", "duplicate"]
        assert Path(rows[0][1]).suffix == ".flac"
    finally:
        connection.close()


@ffmpeg_required
def test_a_report_survives_the_round_trip(tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    original = scan_duplicates(tmp_path)
    loaded = ld.load_report(ld.export_sqlite(original, tmp_path / "index.db"))

    assert loaded.root == original.root
    assert loaded.match == original.match
    assert loaded.stats.files == original.stats.files
    assert len(loaded.files) == len(original.files)
    assert len(loaded.groups) == len(original.groups)
    assert loaded.groups[0].keeper.path == original.groups[0].keeper.path
    assert loaded.reclaimable_bytes == original.reclaimable_bytes


@ffmpeg_required
def test_duplicates_can_be_resolved_from_the_database_alone(tmp_path):
    """The walk and the removal need not happen in the same process."""
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    db = ld.export_sqlite(scan_duplicates(tmp_path), tmp_path / "index.db")
    result = resolve_duplicates(ld.load_report(db), dry_run=False)

    assert len(result.resolved) == 1
    assert (tmp_path / "a.flac").exists()
    assert not (tmp_path / "b.mp3").exists()


@ffmpeg_required
def test_an_index_that_has_gone_stale_removes_nothing(tmp_path):
    """The size and mtime travel in the database, so a file edited after the
    export is still recognised as no longer the one that was indexed."""
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    db = ld.export_sqlite(scan_duplicates(tmp_path), tmp_path / "index.db")
    (tmp_path / "b.mp3").write_bytes(b"something else entirely")

    result = resolve_duplicates(ld.load_report(db), dry_run=False)
    assert result.resolved == []
    assert "changed since the scan" in result.skipped[0].error
    assert (tmp_path / "b.mp3").exists()


def test_the_stored_keeper_wins_over_the_ranking(tmp_path):
    """A database written after someone overrode the keeper must resolve to
    what they chose, not to what rank_key() would pick now."""
    lossless = _entry("/m/a.flac", title="S", artist="A", tier=2)
    lossy = _entry("/m/b.mp3", title="S", artist="A", tier=1, lossless=False)
    group = ld.DuplicateGroup(
        key="tags:a|s", matched_by="tags", files=[lossy, lossless]
    )
    report = ld.DedupReport(root="/m", groups=[group], files=[lossy, lossless])

    loaded = ld.load_report(ld.export_sqlite(report, tmp_path / "index.db"))

    assert loaded.groups[0].keeper.path == "/m/b.mp3"


def test_export_replaces_an_earlier_database(tmp_path):
    report = ld.DedupReport(root="/m", files=[_entry("/m/a.flac")])
    target = tmp_path / "index.db"
    ld.export_sqlite(report, target)
    ld.export_sqlite(ld.DedupReport(root="/m", files=[]), target)

    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    finally:
        connection.close()


def test_loading_refuses_a_database_from_another_schema(tmp_path):
    target = tmp_path / "index.db"
    ld.export_sqlite(ld.DedupReport(root="/m"), target)

    connection = sqlite3.connect(target)
    try:
        connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="schema version 99"):
        ld.load_report(target)


def test_loading_refuses_something_that_is_not_one_of_ours(tmp_path):
    stranger = tmp_path / "not-a.db"
    stranger.write_bytes(b"this is not a database")
    with pytest.raises(ValueError, match="not a SpotiFLAC dedup database"):
        ld.load_report(stranger)


def test_loading_a_missing_database_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        ld.load_report(tmp_path / "nope.db")


@ffmpeg_required
def test_restoring_takes_the_empty_quarantine_folders_with_it(tmp_path):
    """A mirror of the library left standing empty reads as an undo that
    did not work."""
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(
        tmp_path / "deep" / "nest" / "b.mp3",
        codec="libmp3lame",
        title="Song",
        artist="A",
    )

    done = resolve_duplicates(scan_duplicates(tmp_path), dry_run=False)
    trash = tmp_path / ld.TRASH_DIRNAME
    assert (trash / "deep" / "nest").is_dir()

    restore_manifest(done.manifest_path)

    assert (tmp_path / "deep" / "nest" / "b.mp3").exists()
    assert not (trash / "deep").exists()
    # The quarantine root itself stays: it still holds the manifest.
    assert trash.is_dir()
    assert Path(done.manifest_path).exists()

"""Tests for core/db.py, the persistent JobQueue, and core/download_log.py."""

from __future__ import annotations

import time

import pytest

from SpotiFLAC.core import db, download_log
from SpotiFLAC.core.history import HistoryManager
from SpotiFLAC.core.job_queue import JobQueue, JobStatus
from SpotiFLAC.core.models import TrackMetadata


def _wait_until(predicate, timeout=3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true within timeout")


# ── Schema ────────────────────────────────────────────────────────────────


def test_migrations_are_idempotent():
    conn = db.connection()
    before = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]

    # A second process opening the same file must not re-run anything.
    db.reset_for_tests()
    conn = db.connection()
    after = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]

    assert before == after
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert rows == after


def test_dumps_never_raises_on_unserialisable_payload():
    class Opaque:
        __slots__ = ()

    # `default=str` handles most things; the marker path covers what it can't.
    assert isinstance(db.dumps({"x": Opaque()}), str)
    assert db.loads("{not json", fallback={"a": 1}) == {"a": 1}


# ── Job queue persistence ─────────────────────────────────────────────────


def test_submitted_job_is_written_to_the_store():
    q = JobQueue(handler=lambda payload: payload["x"], persist=True)
    job = q.submit("alice", {"x": 1})
    _wait_until(lambda: q.get(job.id).status is JobStatus.DONE)

    row = (
        db.connection().execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    )
    assert row is not None
    assert row["owner"] == "alice"
    assert row["status"] == "done"
    assert row["finished_at"] is not None


def test_unfinished_jobs_are_restored_and_rerun():
    """A queue built over a store with queued work picks it up."""
    # Simulate what a killed process leaves behind: one QUEUED, one RUNNING.
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO jobs (id, owner, payload, status, created_at, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("j-queued", "alice", db.dumps({"n": 1}), "queued", 1.0, None),
                ("j-running", "bob", db.dumps({"n": 2}), "running", 2.0, 2.5),
                ("j-done", "alice", db.dumps({"n": 3}), "done", 3.0, 3.5),
            ],
        )

    seen: list[int] = []
    q = JobQueue(handler=lambda payload: seen.append(payload["n"]), persist=True)

    _wait_until(lambda: len(seen) == 2)
    # Submission order, and the finished one is never re-run.
    assert seen == [1, 2]
    assert q.get("j-done").status is JobStatus.DONE
    assert q.get("j-running").started_at is not None


def test_persistence_is_off_by_default():
    q = JobQueue(handler=lambda _payload: None)
    job = q.submit("alice", {})
    _wait_until(lambda: q.get(job.id).status is JobStatus.DONE)

    rows = db.connection().execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert rows == 0


def test_evicted_jobs_are_dropped_from_the_store():
    q = JobQueue(handler=lambda _p: None, persist=True, max_history=2)
    ids = [q.submit("alice", {"i": i}).id for i in range(5)]
    _wait_until(
        lambda: all(q.get(i) is None or q.get(i).status is JobStatus.DONE for i in ids)
    )
    _wait_until(lambda: len(q.list_all()) <= 2)

    stored = db.connection().execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert stored <= 2


def test_quota_check_can_refuse_a_submission():
    class Refused(RuntimeError):
        pass

    def check(owner: str) -> None:
        if owner == "greedy":
            raise Refused("no more for you")

    q = JobQueue(handler=lambda _p: None, quota_check=check)
    q.submit("alice", {})  # allowed
    with pytest.raises(Refused):
        q.submit("greedy", {})


# ── Download log ──────────────────────────────────────────────────────────


def test_download_log_records_and_counts(tmp_path):
    target = tmp_path / "song.flac"
    target.write_bytes(b"x" * 1024)

    download_log.record(
        owner="alice",
        isrc="ITAAA0000001",
        title="Song",
        artist="Artist",
        provider="ext:tidal-web",
        file_path=str(target),
        fmt="flac",
    )

    assert download_log.count_since("alice", 0) == 1
    assert download_log.bytes_since("alice", 0) == 1024
    assert download_log.has_isrc("itaaa0000001") is True
    assert download_log.has_isrc("ITAAA0000001", owner="bob") is False
    assert download_log.totals("alice") == {"tracks": 1, "bytes": 1024}


def test_download_log_hook_reads_result_and_metadata(tmp_path):
    from SpotiFLAC.core.models import DownloadResult

    target = tmp_path / "hooked.flac"
    target.write_bytes(b"y" * 10)

    hook = download_log.record_hook(owner="bob")
    hook(
        DownloadResult.ok("ext:qobuz", str(target)),
        TrackMetadata(
            id="sp1",
            title="Hooked",
            artists="Someone",
            album="Album",
            album_artist="Someone",
            isrc="GBAAA0000002",
        ),
    )

    recent = download_log.recent(owner="bob")
    assert len(recent) == 1
    assert recent[0].title == "Hooked"
    assert recent[0].provider == "ext:qobuz"
    assert recent[0].isrc == "GBAAA0000002"


def test_failed_downloads_do_not_count_towards_usage():
    download_log.record(owner="alice", isrc="X1", success=False)
    assert download_log.count_since("alice", 0) == 0


# ── History ───────────────────────────────────────────────────────────────


def test_history_imports_a_legacy_json_file_once(tmp_path):
    legacy = tmp_path / "recent-fetches.json"
    legacy.write_text(
        '[{"id": "old-1", "title": "Old", "fetched_at": 5}]', encoding="utf-8"
    )

    manager = HistoryManager(legacy_path=legacy)
    assert [e["id"] for e in manager.get_all()] == ["old-1"]

    # Cleared entries must not come back on the next read.
    manager.clear()
    assert HistoryManager(legacy_path=legacy).get_all() == []


def test_history_keeps_most_recent_first_and_dedupes():
    manager = HistoryManager()
    for idx in range(3):
        manager.add(
            TrackMetadata(
                id=f"t{idx}",
                title=f"Track {idx}",
                artists="A",
                album="B",
                album_artist="A",
            )
        )
        time.sleep(0.01)

    ids = [e["id"] for e in manager.get_all()]
    assert ids[0] == "t2"
    assert len(ids) == 3

    manager.add(
        TrackMetadata(
            id="t0", title="Track 0", artists="A", album="B", album_artist="A"
        )
    )
    ids = [e["id"] for e in manager.get_all()]
    assert ids[0] == "t0"
    assert len(ids) == 3

    assert manager.remove("t0") is True
    assert "t0" not in [e["id"] for e in manager.get_all()]

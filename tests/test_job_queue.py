"""Tests for core/job_queue.py (--web-multiuser's queued downloads)."""

from __future__ import annotations

import threading
import time

from SpotiFLAC.core.job_queue import JobQueue, JobStatus


def _wait_until(predicate, timeout=2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true within timeout")


def test_job_runs_and_records_result():
    q = JobQueue(handler=lambda payload: payload["x"] * 2)
    job = q.submit("alice", {"x": 21})

    _wait_until(lambda: q.get(job.id).status == JobStatus.DONE)
    done = q.get(job.id)
    assert done.result == 42
    assert done.owner == "alice"
    assert done.error is None
    assert done.started_at is not None
    assert done.finished_at is not None


def test_job_failure_is_captured_not_raised():
    def handler(_payload):
        raise RuntimeError("boom")

    q = JobQueue(handler=handler)
    job = q.submit("alice", {})

    _wait_until(lambda: q.get(job.id).status == JobStatus.FAILED)
    failed = q.get(job.id)
    assert failed.error == "boom"
    assert failed.result is None


def test_jobs_run_in_submission_order_with_one_worker():
    order: list[int] = []
    lock = threading.Lock()

    def handler(payload):
        with lock:
            order.append(payload["n"])
        return None

    q = JobQueue(handler=handler, workers=1)
    jobs = [q.submit("alice", {"n": i}) for i in range(10)]

    _wait_until(lambda: all(q.get(j.id).status == JobStatus.DONE for j in jobs))
    assert order == list(range(10))


def test_list_for_filters_by_owner():
    q = JobQueue(handler=lambda p: p)
    j1 = q.submit("alice", {"n": 1})
    j2 = q.submit("bob", {"n": 2})
    j3 = q.submit("alice", {"n": 3})

    _wait_until(lambda: all(q.get(j.id).status == JobStatus.DONE for j in (j1, j2, j3)))

    alice_jobs = q.list_for("alice")
    assert {j.id for j in alice_jobs} == {j1.id, j3.id}
    bob_jobs = q.list_for("bob")
    assert {j.id for j in bob_jobs} == {j2.id}


def test_list_all_is_sorted_by_creation_order():
    q = JobQueue(handler=lambda p: p)
    jobs = [q.submit("alice", {"n": i}) for i in range(5)]
    _wait_until(lambda: all(q.get(j.id).status == JobStatus.DONE for j in jobs))

    all_jobs = q.list_all()
    assert [j.id for j in all_jobs] == [j.id for j in jobs]


def test_get_unknown_job_returns_none():
    q = JobQueue(handler=lambda p: p)
    assert q.get("does-not-exist") is None


def test_job_to_dict_shape():
    q = JobQueue(handler=lambda p: p)
    job = q.submit("alice", {"url": "https://example.com"})
    _wait_until(lambda: q.get(job.id).status == JobStatus.DONE)

    d = q.get(job.id).to_dict()
    assert d["owner"] == "alice"
    assert d["status"] == "done"
    assert d["payload"] == {"url": "https://example.com"}
    assert "result" not in d  # deliberately omitted — see Job.to_dict()


def test_multiple_workers_process_concurrently():
    started = threading.Event()
    release = threading.Event()
    concurrent_count = {"value": 0, "max": 0}
    lock = threading.Lock()

    def handler(_payload):
        with lock:
            concurrent_count["value"] += 1
            concurrent_count["max"] = max(
                concurrent_count["max"], concurrent_count["value"]
            )
        started.set()
        release.wait(timeout=2.0)
        with lock:
            concurrent_count["value"] -= 1
        return None

    q = JobQueue(handler=handler, workers=3)
    jobs = [q.submit("alice", {}) for _ in range(3)]
    started.wait(timeout=2.0)
    time.sleep(0.1)  # let all 3 workers pick up a job
    release.set()

    _wait_until(lambda: all(q.get(j.id).status == JobStatus.DONE for j in jobs))
    assert concurrent_count["max"] >= 2

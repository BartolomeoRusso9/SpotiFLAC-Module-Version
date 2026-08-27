"""core/job_queue.py — a small, generic, thread-safe job queue.

Backs `--web-multiuser`'s queued downloads (see webapp.py): instead of
every submitted download spawning its own unbounded background thread (the
single-user default's behavior, unchanged), submissions are queued and a
fixed number of workers drain them one at a time, each job tagged with
whoever submitted it so a user can list just their own.

Deliberately generic and NOT wired into the download machinery itself —
the worker function it calls is passed in by the caller (webapp.py passes
a closure that calls the existing, unmodified SpotiFLAC_API.download_tracks()).
That keeps this an additive wrapper around the download path rather than a
rewrite of it: everything this module knows how to do is "run this
callable, later, in order, and remember what happened."
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    owner: str
    payload: dict
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: object = None
    error: str | None = None

    def to_dict(self) -> dict:
        # `result` is deliberately excluded: it's whatever the handler
        # returned, not guaranteed JSON-serializable (webapp.py's handler
        # returns download_tracks()'s own result shape) — read job.result
        # directly if you need it in-process; this projection is only the
        # part that's always safe to hand back over HTTP as-is.
        return {
            "id": self.id,
            "owner": self.owner,
            "payload": self.payload,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class JobQueue:
    """FIFO queue processed by `workers` background daemon threads.

    `handler(payload: dict) -> Any` is called for every job; its return
    value becomes `job.result`, an exception becomes `job.error` (and
    status FAILED) rather than killing the worker thread.
    """

    def __init__(
        self,
        handler: Callable[[dict], object],
        *,
        workers: int = 1,
    ) -> None:
        self._handler = handler
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._worker_loop, daemon=True)
            for _ in range(max(1, workers))
        ]
        for t in self._threads:
            t.start()

    def submit(self, owner: str, payload: dict) -> Job:
        job = Job(id=uuid.uuid4().hex, owner=owner, payload=payload)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at)

    def list_for(self, owner: str) -> list[Job]:
        return [j for j in self.list_all() if j.owner == owner]

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                with self._lock:
                    job = self._jobs.get(job_id)
                if job is None:
                    continue

                job.status = JobStatus.RUNNING
                job.started_at = time.time()
                try:
                    job.result = self._handler(job.payload)
                    job.status = JobStatus.DONE
                except Exception as exc:
                    job.status = JobStatus.FAILED
                    job.error = str(exc)
                    logger.exception("[JobQueue] Job %s failed", job_id)
                finally:
                    job.finished_at = time.time()
            finally:
                self._queue.task_done()

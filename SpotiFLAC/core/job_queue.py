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
from dataclasses import dataclass, field, replace
from enum import Enum

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


_FINISHED_STATUSES = frozenset({JobStatus.DONE, JobStatus.FAILED})


class QueueFullError(RuntimeError):
    """Raised by submit() when an owner is over their pending-job limit.

    Carries `pending` and `limit` as attributes rather than only inside the
    message. An HTTP caller should be told how many jobs are queued and what
    the limit is — that is genuinely useful — but it should be told by a
    response built from these fields, never by putting `str(exc)` in the
    body. That pattern is fine until the day an exception carries something
    it shouldn't, and by then nobody is looking.
    """

    def __init__(self, message: str, pending: int = 0, limit: int = 0) -> None:
        super().__init__(message)
        self.pending = pending
        self.limit = limit


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

    #: Finished jobs kept for history, oldest evicted first. Without a bound,
    #: `_jobs` is a dict that only ever grows — fine for a session, a slow
    #: leak for the long-running server this exists to serve.
    DEFAULT_MAX_HISTORY = 500
    #: Queued-but-not-yet-started jobs a single account may have outstanding.
    #: Anyone logged in can call submit-download in a loop; this keeps one
    #: account from filling the queue for everybody else.
    DEFAULT_MAX_PENDING_PER_OWNER = 50

    def __init__(
        self,
        handler: Callable[[dict], object],
        *,
        workers: int = 1,
        max_history: int = DEFAULT_MAX_HISTORY,
        max_pending_per_owner: int = DEFAULT_MAX_PENDING_PER_OWNER,
    ) -> None:
        self._handler = handler
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._max_history = max_history
        self._max_pending_per_owner = max_pending_per_owner
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._worker_loop, daemon=True)
            for _ in range(max(1, workers))
        ]
        for t in self._threads:
            t.start()

    def submit(self, owner: str, payload: dict) -> Job:
        """Queues a job for `owner`.

        Raises QueueFullError if that owner already has
        `max_pending_per_owner` jobs waiting to start.
        """
        job = Job(id=uuid.uuid4().hex, owner=owner, payload=payload)
        with self._lock:
            pending = sum(
                1
                for j in self._jobs.values()
                if j.owner == owner and j.status is JobStatus.QUEUED
            )
            if pending >= self._max_pending_per_owner:
                msg = (
                    f"{owner} already has {pending} downloads queued "
                    f"(limit {self._max_pending_per_owner}); wait for some to finish."
                )
                raise QueueFullError(
                    msg, pending=pending, limit=self._max_pending_per_owner
                )
            self._jobs[job.id] = job
            self._evict_finished_locked()
        self._queue.put(job.id)
        return job

    def _evict_finished_locked(self) -> None:
        """Drops the oldest finished jobs once history exceeds the cap.

        Only DONE/FAILED entries are eligible: anything queued or running is
        still live state, however old it is. Caller must hold `_lock`.
        """
        if len(self._jobs) <= self._max_history:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status in _FINISHED_STATUSES),
            key=lambda j: j.finished_at or j.created_at,
        )
        for job in finished[: len(self._jobs) - self._max_history]:
            self._jobs.pop(job.id, None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return replace(job) if job is not None else None

    def list_all(self) -> list[Job]:
        # Snapshots, not the live Job objects. Holding the lock only for the
        # duration of the sort would hand the caller references a worker is
        # still writing to, so a reader that looked at `status` and then at
        # `finished_at` could see a job that is DONE with no finish time —
        # exactly the tear the worker takes the lock to avoid. Copying under
        # the lock makes each returned Job internally consistent. `payload`
        # is shared rather than deep-copied: it is written once in submit()
        # and never mutated afterwards.
        with self._lock:
            return [
                replace(job)
                for job in sorted(self._jobs.values(), key=lambda j: j.created_at)
            ]

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

                with self._lock:
                    job.status = JobStatus.RUNNING
                    job.started_at = time.time()

                result = error = None
                status = JobStatus.DONE
                try:
                    result = self._handler(job.payload)
                except Exception as exc:
                    status = JobStatus.FAILED
                    error = str(exc)
                    logger.exception("[JobQueue] Job %s failed", job_id)

                # The transition to a terminal state happens under the lock
                # that list_all() reads under, so a reader can never observe
                # a job that is DONE but has no finished_at. Eviction runs
                # here as well as in submit(): a queue that is drained and
                # then goes quiet would otherwise keep every completed job
                # until somebody happened to submit another one.
                with self._lock:
                    job.result = result
                    job.error = error
                    job.status = status
                    job.finished_at = time.time()
                    self._evict_finished_locked()
            finally:
                self._queue.task_done()

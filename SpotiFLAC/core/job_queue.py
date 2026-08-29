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

Persistence (`persist=True`)
----------------------------
In-memory remains the default and the whole behaviour of the class is
unchanged without the flag. With it, every state transition is mirrored into
`core/db.py`, and the constructor restores whatever was left unfinished by
the previous process — which is the case this queue exists for: a headless
instance restarted (a container update, a NAS reboot) used to silently drop
every download somebody had queued.

A job found in RUNNING at startup is put back to QUEUED rather than left
where it was: the thread that was running it died with the process, so the
only honest reading of that row is "started, outcome unknown". Handlers must
therefore tolerate being run twice for the same payload — the download path
does, because every provider skips a track whose file is already on disk.
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
        persist: bool = False,
        quota_check: Callable[[str], None] | None = None,
    ) -> None:
        self._handler = handler
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._max_history = max_history
        self._max_pending_per_owner = max_pending_per_owner
        self._persist = persist
        # Called with the owner just before a job is accepted; it raises to
        # refuse. Injected rather than imported so this module keeps knowing
        # nothing about accounts — see webapp.py, which passes the
        # web_users-backed check.
        self._quota_check = quota_check
        self._lock = threading.Lock()
        # Guards the write+forget store mutations that deliberately run outside
        # `_lock` (they do I/O). Without it, two threads' `_write`/`_forget`
        # pairs can interleave so an evicted job is deleted before its own
        # write lands, leaving an orphan row that nothing ever cleans up.
        self._persist_lock = threading.Lock()

        # Restore before the workers start, so a recovered job cannot be
        # picked up while the rest of the backlog is still being read.
        restored: list[str] = []
        if persist:
            restored = self._restore()

        self._threads = [
            threading.Thread(target=self._worker_loop, daemon=True)
            for _ in range(max(1, workers))
        ]
        for t in self._threads:
            t.start()
        for job_id in restored:
            self._queue.put(job_id)

    # ── Persistence ───────────────────────────────────────────────────────

    def _restore(self) -> list[str]:
        """Reads back everything the previous process left behind.

        Returns the ids that need re-queueing, oldest first. Never raises: a
        database that cannot be read costs the backlog, and that is strictly
        better than refusing to start the queue at all.
        """
        from . import db

        try:
            rows = (
                db.connection()
                .execute("SELECT * FROM jobs ORDER BY created_at")
                .fetchall()
            )
        except Exception:
            logger.warning("[JobQueue] Could not restore jobs", exc_info=True)
            return []

        to_run: list[str] = []
        for row in rows:
            try:
                status = JobStatus(row["status"])
            except ValueError:
                status = JobStatus.QUEUED
            job = Job(
                id=row["id"],
                owner=row["owner"] or "",
                payload=db.loads(row["payload"], {}) or {},
                status=status,
                created_at=float(row["created_at"] or 0.0),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error=row["error"],
            )
            if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                # See the module docstring: RUNNING means "the process died
                # mid-job", which is indistinguishable from queued as far as
                # what still has to happen.
                job.status = JobStatus.QUEUED
                job.started_at = None
                to_run.append(job.id)
            self._jobs[job.id] = job

        if to_run:
            logger.info("[JobQueue] Restored %d unfinished job(s)", len(to_run))
            self._write_many(to_run)
        return to_run

    def _write(self, job: Job) -> None:
        """Mirrors one job's current state into the store. Never raises."""
        if not self._persist:
            return
        from . import db

        try:
            with db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, owner, payload, status,
                        created_at, started_at, finished_at, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status      = excluded.status,
                        started_at  = excluded.started_at,
                        finished_at = excluded.finished_at,
                        error       = excluded.error
                    """,
                    (
                        job.id,
                        job.owner,
                        db.dumps(job.payload),
                        job.status.value,
                        job.created_at,
                        job.started_at,
                        job.finished_at,
                        job.error,
                    ),
                )
        except Exception:
            logger.debug("[JobQueue] Could not persist job %s", job.id, exc_info=True)

    def _write_many(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            job = self._jobs.get(job_id)
            if job is not None:
                self._write(job)

    def _forget(self, job_ids: list[str]) -> None:
        if not self._persist or not job_ids:
            return
        from . import db

        try:
            with db.transaction() as conn:
                conn.executemany(
                    "DELETE FROM jobs WHERE id = ?", [(i,) for i in job_ids]
                )
        except Exception:
            logger.debug("[JobQueue] Could not evict persisted jobs", exc_info=True)

    def submit(self, owner: str, payload: dict) -> Job:
        """Queues a job for `owner`.

        Raises QueueFullError if that owner already has
        `max_pending_per_owner` jobs waiting to start, or whatever
        `quota_check` raises if one was supplied and refuses.
        """
        if self._quota_check is not None:
            # Deliberately outside the lock: the check may read the database,
            # and holding the queue lock across that would block every worker
            # transition for the duration.
            self._quota_check(owner)

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
            evicted = self._evict_finished_locked()
        with self._persist_lock:
            self._write(job)
            self._forget(evicted)
        self._queue.put(job.id)
        return job

    def _evict_finished_locked(self) -> list[str]:
        """Drops the oldest finished jobs once history exceeds the cap.

        Only DONE/FAILED entries are eligible: anything queued or running is
        still live state, however old it is. Caller must hold `_lock`.
        Returns the evicted ids so the caller can drop them from the store
        too — outside the lock, since that is I/O.
        """
        if len(self._jobs) <= self._max_history:
            return []
        finished = sorted(
            (j for j in self._jobs.values() if j.status in _FINISHED_STATUSES),
            key=lambda j: j.finished_at or j.created_at,
        )
        evicted: list[str] = []
        for job in finished[: len(self._jobs) - self._max_history]:
            self._jobs.pop(job.id, None)
            evicted.append(job.id)
        return evicted

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
                self._write(job)

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
                    evicted = self._evict_finished_locked()
                with self._persist_lock:
                    self._write(job)
                    self._forget(evicted)
            finally:
                self._queue.task_done()

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.repositories import JobRepository


class QueueService:
    """Minimal persistent-job abstraction for the refactor baseline.

    The service keeps a tiny in-memory registry and optionally writes each job to
    the SQLite-backed repository so the queue survives process restarts.
    """

    def __init__(self, repo: JobRepository | None = None) -> None:
        self._repo = repo
        self._jobs: MutableMapping[str, dict[str, Any]] = {}

    async def enqueue(self, request: DownloadRequest) -> dict[str, Any]:
        job_id = f"job-{uuid4().hex}"
        job = {
            "id": job_id,
            "status": "QUEUED",
            "request": asdict(request),
            "total_items": len(request.sources),
            "completed_items": 0,
            "priority": 0,
            "source": request.sources[0] if request.sources else "",
        }
        self._jobs[job_id] = job
        if self._repo is not None:
            self._repo.create({
                "id": job_id,
                "source": job["source"],
                "status": job["status"],
                "payload": {"request": asdict(request), "total_items": len(request.sources)},
                "total_items": len(request.sources),
                "completed_items": 0,
                "priority": 0,
            })
        return self._jobs[job_id].copy()

    async def pause(self, job_id: str) -> dict[str, Any]:
        job = self._jobs[job_id]
        job["status"] = "PAUSED"
        if self._repo is not None:
            self._repo.update_status(job_id, "PAUSED")
        return job.copy()

    async def resume(self, job_id: str) -> dict[str, Any]:
        job = self._jobs[job_id]
        job["status"] = "QUEUED"
        if self._repo is not None:
            self._repo.update_status(job_id, "QUEUED")
        return job.copy()

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self._jobs[job_id]
        job["status"] = "CANCELLED"
        if self._repo is not None:
            self._repo.update_status(job_id, "CANCELLED")
        return job.copy()

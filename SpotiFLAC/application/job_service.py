from __future__ import annotations

from typing import Any

from SpotiFLAC.application.download_service import DownloadService
from SpotiFLAC.application.queue_service import QueueService
from SpotiFLAC.core.config import DownloadRequest
from SpotiFLAC.core.repositories import JobRepository


class JobService:
    """Concrete application service that owns queue state and repository persistence."""

    def __init__(
        self,
        repo: JobRepository | None = None,
        download_service: DownloadService | None = None,
    ) -> None:
        self._repo = repo or JobRepository()
        self._queue = QueueService(repo=self._repo)
        self._download_service = download_service or DownloadService()
        self._requests: dict[str, DownloadRequest] = {}

    async def enqueue(self, request: DownloadRequest) -> dict[str, Any]:
        job = await self._queue.enqueue(request)
        self._requests[job["id"]] = request
        return job

    async def execute(self, job_id: str) -> Any:
        request = self._requests[job_id]
        if self.get(job_id)["status"] == "CANCELLED":
            return None
        self._repo.update_status(job_id, "RUNNING")
        try:
            report = await self._download_service.download(request)
        except Exception:
            self._repo.update_status(job_id, "FAILED")
            return None
        self._repo.update_status(job_id, "DONE")
        return report

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return await self._queue.cancel(job_id)

    async def retry(self, job_id: str) -> dict[str, Any]:
        status = self.get(job_id)["status"]
        if status not in {"FAILED", "CANCELLED"}:
            raise ValueError(f"job {job_id} is not retryable")
        return await self._queue.resume(job_id)

    def get(self, job_id: str) -> dict:
        return self._repo.get(job_id)

    def list(self) -> list[dict]:
        return self._repo.list()

    async def pause(self, job_id: str) -> dict[str, Any]:
        return await self._queue.pause(job_id)

    async def resume(self, job_id: str) -> dict[str, Any]:
        return await self._queue.resume(job_id)

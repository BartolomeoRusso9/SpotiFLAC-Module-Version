from __future__ import annotations

import asyncio
from typing import Any
from pathlib import Path

from SpotiFLAC.application.download_service import DownloadService
from SpotiFLAC.application.event_bus import EventBus
from SpotiFLAC.application.queue_service import QueueService
from SpotiFLAC.core.config import DownloadRequest, SpotiFLACConfig
from SpotiFLAC.core.repositories import JobRepository


class JobService:
    """Concrete application service that owns queue state and repository persistence."""

    def __init__(
        self,
        repo: JobRepository | None = None,
        download_service: DownloadService | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._repo = repo or JobRepository()
        self._queue = QueueService(repo=self._repo)
        self._download_service = download_service or DownloadService()
        self._event_bus = event_bus or EventBus()
        self._requests: dict[str, DownloadRequest] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def enqueue(self, request: DownloadRequest) -> dict[str, Any]:
        job = await self._queue.enqueue(request)
        self._requests[job["id"]] = request
        await self._event_bus.publish("job.created", {"job_id": job["id"]})
        return job

    async def execute(self, job_id: str) -> Any:
        request = self._requests.get(job_id) or self._request_from_job(job_id)
        self._requests[job_id] = request
        if self.get(job_id)["status"] == "CANCELLED":
            return None
        task = asyncio.current_task()
        if task is not None:
            self._tasks[job_id] = task
        self._repo.update_status(job_id, "RUNNING")
        await self._event_bus.publish("job.started", {"job_id": job_id})
        try:
            report = await self._download_service.download(request)
        except asyncio.CancelledError:
            self._repo.update_status(job_id, "CANCELLED")
            await self._event_bus.publish("job.cancelled", {"job_id": job_id})
            raise
        except Exception:
            self._repo.update_status(job_id, "FAILED")
            await self._event_bus.publish("job.failed", {"job_id": job_id})
            return None
        finally:
            self._tasks.pop(job_id, None)
        self._repo.update_progress(
            job_id,
            getattr(report, "total", len(request.sources)),
        )
        self._repo.update_status(job_id, "DONE")
        await self._event_bus.publish("job.completed", {"job_id": job_id})
        return report

    def _request_from_job(self, job_id: str) -> DownloadRequest:
        payload = self._repo.get(job_id).get("request", {})
        config_data = payload.get("config", {})
        config = SpotiFLACConfig()
        for section_name in (
            "download",
            "metadata",
            "lyrics",
            "extensions",
            "queue",
            "security",
        ):
            section = config_data.get(section_name, {})
            target = getattr(config, section_name)
            for key, value in section.items():
                if hasattr(target, key):
                    setattr(target, key, value)
        output = config_data.get("output", {})
        for key, value in output.items():
            if hasattr(config.output, key):
                setattr(config.output, key, Path(value) if key == "directory" else value)
        return DownloadRequest(
            sources=list(payload.get("sources", [])),
            config=config,
            prefetched=None,
        )

    async def cancel(self, job_id: str) -> dict[str, Any]:
        task = self._tasks.get(job_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            return self._repo.get(job_id)
        job = await self._queue.cancel(job_id)
        await self._event_bus.publish("job.cancelled", {"job_id": job_id})
        return job

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

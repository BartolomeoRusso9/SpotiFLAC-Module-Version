from __future__ import annotations

import asyncio
import time

from SpotiFLAC.application.job_service import JobService
from SpotiFLAC.application.event_bus import EventBus
from SpotiFLAC.core.config import SpotiFLACConfig, DownloadRequest


class ApiAdapter:
    """Minimal adapter for the REST/CLI boundary.

    It converts a simple request payload into the internal app-layer contract and
    returns a small, structured job description suitable for different frontends.
    """

    def __init__(
        self,
        job_service: JobService | None = None,
        start_background: bool = False,
        event_bus: EventBus | None = None,
    ) -> None:
        self._event_bus = event_bus or EventBus()
        self._job_service = job_service or JobService(event_bus=self._event_bus)
        self._start_background = start_background
        self._tasks: set[asyncio.Task] = set()

    async def submit_download(self, payload: dict) -> dict:
        config = SpotiFLACConfig()
        config.download.quality = payload.get("quality", "LOSSLESS")

        sources = payload.get("sources") or []
        if not sources and payload.get("url"):
            sources = [payload["url"]]

        request = DownloadRequest(
            sources=sources,
            config=config,
        )
        job = await self._job_service.enqueue(request)
        if self._start_background:
            task = asyncio.create_task(self._job_service.execute(job["id"]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return {
            "id": job["id"],
            "status": job["status"],
            "provider_order": ["tidal", "qobuz", "deezer", "apple", "amazon"],
            "items": len(request.sources),
            "items_detail": self._job_service.items(job["id"]),
        }

    async def list_downloads(self) -> list[dict]:
        return [
            self._job_view(job, self._job_service.items(job["id"]))
            for job in self._job_service.list()
        ]

    async def get_download(self, job_id: str) -> dict | None:
        try:
            return self._job_view(
                self._job_service.get(job_id),
                self._job_service.items(job_id),
            )
        except KeyError:
            return None

    async def pause_download(self, job_id: str) -> dict | None:
        try:
            await self._job_service.pause(job_id)
            return await self.get_download(job_id)
        except KeyError:
            return None

    async def resume_download(self, job_id: str) -> dict | None:
        try:
            await self._job_service.resume(job_id)
            return await self.get_download(job_id)
        except KeyError:
            return None

    async def cancel_download(self, job_id: str) -> dict | None:
        try:
            await self._job_service.cancel(job_id)
            return await self.get_download(job_id)
        except KeyError:
            return None

    async def retry_download(self, job_id: str) -> dict | None:
        try:
            await self._job_service.retry(job_id)
            return await self.get_download(job_id)
        except KeyError:
            return None

    @staticmethod
    def _job_view(job: dict, items: list[dict] | None = None) -> dict:
        request = job.get("request", {})
        return {
            "id": str(job["id"]),
            "owner": "",
            "status": str(job.get("status", "QUEUED")).lower(),
            "created_at": job.get("created_at") or time.time(),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "error": job.get("error"),
            "payload": {
                "url": job.get("source", ""),
                "items": job.get("total_items", len(request.get("sources", []))),
                "completed_items": job.get("completed_items", 0),
                "priority": job.get("priority", 0),
                "items_detail": items or [],
            },
        }

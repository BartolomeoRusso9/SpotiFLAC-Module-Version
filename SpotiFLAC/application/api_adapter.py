from __future__ import annotations

import asyncio
import time

from SpotiFLAC.application.job_service import JobService
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
    ) -> None:
        self._job_service = job_service or JobService()
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
        }

    async def list_downloads(self) -> list[dict]:
        return [self._job_view(job) for job in self._job_service.list()]

    async def get_download(self, job_id: str) -> dict | None:
        try:
            return self._job_view(self._job_service.get(job_id))
        except KeyError:
            return None

    @staticmethod
    def _job_view(job: dict) -> dict:
        request = job.get("request", {})
        return {
            "id": str(job["id"]),
            "owner": "",
            "status": str(job.get("status", "QUEUED")).lower(),
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "payload": {
                "url": job.get("source", ""),
                "items": job.get("total_items", len(request.get("sources", []))),
            },
        }

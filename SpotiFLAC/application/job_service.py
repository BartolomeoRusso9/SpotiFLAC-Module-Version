from __future__ import annotations

import asyncio
import inspect
from typing import Any, List as TypingList
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
        self._resume_events: dict[str, asyncio.Event] = {}

    async def enqueue(self, request: DownloadRequest) -> dict[str, Any]:
        job = await self._queue.enqueue(request)
        self._requests[job["id"]] = request
        resume_event = asyncio.Event()
        resume_event.set()
        self._resume_events[job["id"]] = resume_event
        items = self._repo.create_items(job["id"], list(request.sources))
        await self._event_bus.publish("job.created", {"job_id": job["id"]})
        for item in items:
            await self._event_bus.publish(
                "job.item.created",
                {"job_id": job["id"], "item": item},
            )
        return job

    async def execute(self, job_id: str) -> Any:
        request = self._requests.get(job_id) or self._request_from_job(job_id)
        self._requests[job_id] = request
        if self.get(job_id)["status"] in {"CANCELLED", "PAUSED"}:
            return None
        task = asyncio.current_task()
        if task is not None:
            self._tasks[job_id] = task
        self._repo.update_status(job_id, "RUNNING")
        for item in self._repo.list_items(job_id):
            if item["status"] == "RUNNING":
                self._repo.update_item(item["id"], status="QUEUED")
            if item["status"] == "QUEUED":
                self._repo.update_item(
                    item["id"],
                    status="RUNNING",
                    attempts=item["attempts"] + 1,
                )
        attempt_id = self._repo.create_attempt(job_id)
        await self._event_bus.publish("job.started", {"job_id": job_id})
        try:
            download = self._download_service.download
            if "resume_event" in inspect.signature(download).parameters:
                report = await download(
                    request,
                    resume_event=self._resume_events.setdefault(
                        job_id, self._ready_event()
                    ),
                )
            else:
                report = await download(request)
        except asyncio.CancelledError:
            await self._mark_active_items(job_id, "CANCELLED")
            self._repo.finish_attempt(attempt_id, "CANCELLED")
            self._repo.update_status(job_id, "CANCELLED")
            self._repo.update_error(job_id, "cancelled")
            await self._event_bus.publish("job.cancelled", {"job_id": job_id})
            raise
        except Exception as exc:
            await self._mark_active_items(job_id, "FAILED", error=str(exc))
            self._repo.finish_attempt(attempt_id, "FAILED")
            self._repo.update_status(job_id, "FAILED")
            self._repo.update_error(job_id, str(exc))
            await self._event_bus.publish("job.failed", {"job_id": job_id})
            return None
        finally:
            self._tasks.pop(job_id, None)
        await self._persist_item_results(job_id, report)
        self._repo.update_progress(
            job_id,
            getattr(report, "total", len(request.sources)),
        )
        has_failures = bool(getattr(report, "failed", []))
        terminal_status = "FAILED" if has_failures else "DONE"
        self._repo.finish_attempt(
            attempt_id,
            "COMPLETED",
            (getattr(report, "failed", [])[0].reason if has_failures else None),
        )
        self._repo.update_status(job_id, terminal_status)
        if has_failures:
            self._repo.update_error(job_id, report.failed[0].reason)
            await self._event_bus.publish("job.failed", {"job_id": job_id})
        else:
            await self._event_bus.publish("job.completed", {"job_id": job_id})
        return report

    async def _persist_item_results(self, job_id: str, report: Any) -> None:
        outcomes: dict[str, dict[str, Any]] = {}
        for result in getattr(report, "succeeded", []):
            if result.source:
                outcomes[result.source] = {
                    "status": "DONE",
                    "provider": result.provider,
                    "result": result.file_path,
                }
        for failure in getattr(report, "failed", []):
            if failure.source:
                outcomes[failure.source] = {
                    "status": "FAILED",
                    "provider": failure.provider,
                    "error": failure.reason,
                    "attempts": failure.attempts,
                }
        for item in self._repo.list_items(job_id):
            outcome = outcomes.get(item["source"])
            if outcome:
                updated = self._repo.update_item(item["id"], **outcome)
                await self._event_bus.publish(
                    "job.item.updated",
                    {"job_id": job_id, "item": updated},
                )
                event_name = {
                    "DONE": "completed",
                    "FAILED": "failed",
                    "CANCELLED": "cancelled",
                }[outcome["status"]]
                await self._event_bus.publish(
                    f"job.item.{event_name}",
                    {"job_id": job_id, "item": updated},
                )

    async def _mark_active_items(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        for item in self._repo.list_items(job_id):
            if item["status"] == "RUNNING":
                updated = self._repo.update_item(item["id"], status=status, error=error)
                await self._event_bus.publish(
                    "job.item.updated",
                    {"job_id": job_id, "item": updated},
                )
                event_name = {
                    "DONE": "completed",
                    "FAILED": "failed",
                    "CANCELLED": "cancelled",
                }[status]
                await self._event_bus.publish(
                    f"job.item.{event_name}",
                    {"job_id": job_id, "item": updated},
                )

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
                setattr(
                    config.output, key, Path(value) if key == "directory" else value
                )
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
        self._repo.reset_items_for_retry(job_id)
        self._repo.update_error(job_id, None)
        self._repo.update_status(job_id, "RETRYING")
        self._resume_events.setdefault(job_id, self._ready_event()).set()
        await self._event_bus.publish("job.retrying", {"job_id": job_id})
        return await self._queue.resume(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        return self._repo.get(job_id)

    def list(self) -> TypingList[dict[str, Any]]:
        return self._repo.list()

    def items(self, job_id: str) -> TypingList[dict[str, Any]]:
        return self._repo.list_items(job_id)

    async def pause(self, job_id: str) -> dict[str, Any]:
        event = self._resume_events.setdefault(job_id, self._ready_event())
        event.clear()
        job = await self._queue.pause(job_id)
        await self._event_bus.publish("job.paused", {"job_id": job_id})
        return job

    async def resume(self, job_id: str) -> dict[str, Any]:
        event = self._resume_events.setdefault(job_id, self._ready_event())
        event.set()
        job = await self._queue.resume(job_id)
        await self._event_bus.publish("job.resumed", {"job_id": job_id})
        return job

    @staticmethod
    def _ready_event() -> asyncio.Event:
        event = asyncio.Event()
        event.set()
        return event

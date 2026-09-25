from __future__ import annotations

import asyncio


async def wait_until_resumed(resume_event: asyncio.Event | None) -> None:
    """Wait at a safe boundary while a job's resume gate is closed."""

    if resume_event is not None:
        await resume_event.wait()

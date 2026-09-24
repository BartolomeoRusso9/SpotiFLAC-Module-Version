from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from SpotiFLAC.application.pause import wait_until_resumed
from SpotiFLAC.core.models import DownloadResult
from SpotiFLAC.core.retry import RetryPolicy


class ProviderExecutor:
    """Application-layer adapter for provider execution.

    The legacy downloader still knows how to execute a provider call, but the
    application layer owns the contract: providers are selected by the resolver,
    then executed by this object through a stable, injectable boundary.
    """

    def __init__(
        self,
        executor: Callable[[str, str], Awaitable[DownloadResult | None]] | None = None,
    ) -> None:
        self._executor = executor
        self._last_result: DownloadResult | None = None

    @property
    def last_result(self) -> DownloadResult | None:
        return self._last_result

    async def execute(self, provider: str, source: str) -> None:
        self._last_result = None
        if self._executor is None:
            return None
        result = await self._executor(provider, source)
        if isinstance(result, DownloadResult):
            self._last_result = result

    async def execute_with_retry(
        self,
        provider: str,
        source: str,
        policy: RetryPolicy,
        timeout_s: int | None = None,
        resume_event: asyncio.Event | None = None,
    ) -> Exception | None:
        for _attempt in range(policy.attempts):
            await wait_until_resumed(resume_event)
            try:
                operation = self.execute(provider, source)
                if timeout_s and timeout_s > 0:
                    await asyncio.wait_for(operation, timeout=timeout_s)
                else:
                    await operation
                await wait_until_resumed(resume_event)
                return None
            except Exception as exc:
                if not self.is_retryable(exc, policy):
                    return exc
                last_error = exc
        return last_error

    @staticmethod
    def is_retryable(error: BaseException, policy: RetryPolicy | None = None) -> bool:
        return (policy or RetryPolicy()).is_retryable(error)

    async def __call__(self, provider: str, source: str) -> None:
        await self.execute(provider, source)

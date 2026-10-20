from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from mornye.application.pause import wait_until_resumed
from mornye.core.models import DownloadResult
from mornye.core.retry import RetryPolicy


class ProviderResultError(Exception):
    """A provider completed normally but returned a failed result.

    Providers use ``DownloadResult`` for expected failures instead of raising.
    The application retry boundary still needs to see those failures so it can
    try the next provider without mistaking the result for a success.
    """

    def __init__(self, result: DownloadResult) -> None:
        """Retain the failed result and expose its error as the exception message."""
        self.result = result
        super().__init__(result.error or "provider returned a failed result")

    def is_retryable(self) -> bool:
        """Allow fallback to another provider after an unsuccessful result."""
        # A failed provider result is eligible for the next provider candidate;
        # provider-specific retry policy is handled inside the provider.
        return True


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
        """Run the provider with retries, returning the final error or None.

        Apply timeout_s to each attempt and honor resume_event when supplied.
        A failed structured result returns immediately for provider fallback.
        """
        last_error: Exception | None = None

        for _attempt in range(policy.attempts):
            await wait_until_resumed(resume_event)
            try:
                operation = self.execute(provider, source)
                if timeout_s and timeout_s > 0:
                    await asyncio.wait_for(operation, timeout=timeout_s)
                else:
                    await operation
                await wait_until_resumed(resume_event)
                if self._last_result is not None and not self._last_result.success:
                    return ProviderResultError(self._last_result)
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

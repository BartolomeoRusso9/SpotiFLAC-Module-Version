from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1

    @property
    def attempts(self) -> int:
        return max(1, self.max_attempts)

    def is_retryable(self, error: BaseException) -> bool:
        return not isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))

__all__ = ["RetryPolicy"]

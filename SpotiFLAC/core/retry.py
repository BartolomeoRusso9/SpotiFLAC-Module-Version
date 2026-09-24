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
        if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            return False
        classified = getattr(error, "is_retryable", None)
        if callable(classified):
            return bool(classified())
        return True


__all__ = ["RetryPolicy"]

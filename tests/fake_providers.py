from __future__ import annotations


class FakeProviderError(RuntimeError):
    pass


class FakeProvider:
    def __init__(self, name: str, *, failures: int = 0, error: str = "unavailable"):
        self.name = name
        self.failures = failures
        self.error = error
        self.calls = 0

    async def download(self, source: str) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise FakeProviderError(self.error)

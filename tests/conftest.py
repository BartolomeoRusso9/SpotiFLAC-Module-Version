import json
from typing import Callable, Iterable

import httpx
import pytest

from SpotiFLAC.core.http import NetworkManager


def _make_handler(sequence: Iterable[tuple[str, int, dict]]):
    """Create a MockTransport handler from a sequence of (match_substr, status, payload).
    It will return the first matching response for which match_substr in url."""
    seq = list(sequence)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Consume the first matching entry so repeated calls can be sequenced
        for idx, (match_substr, status, payload) in enumerate(list(seq)):
            if match_substr in url:
                # remove used entry
                try:
                    seq.pop(idx)
                except Exception:
                    pass
                if isinstance(payload, (dict, list)):
                    content = json.dumps(payload).encode()
                    return httpx.Response(status_code=status, content=content, headers={"Content-Type": "application/json"})
                else:
                    return httpx.Response(status_code=status, content=str(payload).encode())
        return httpx.Response(status_code=404, content=b"Not Found")

    return handler


@pytest.fixture
def mock_network_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[Iterable[tuple[str, int, dict]]], None]:
    """Fixture that lets tests install a mock httpx.AsyncClient into NetworkManager.

    Usage:
        mock_network_client([ ("/search", 200, { ... }), ("/track", 200, { ... }) ])
    """

    def _installer(sequence: Iterable[tuple[str, int, dict]]):
        handler = _make_handler(sequence)
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        async def _fake_get_async_client_safe() -> httpx.AsyncClient:
            return client

        monkeypatch.setattr(NetworkManager, "get_async_client_safe", staticmethod(_fake_get_async_client_safe))

    return _installer

import asyncio
from types import SimpleNamespace

from SpotiFLAC.core.signed_session_mobile import perform_signed_fetch


class DummyClient:
    def __init__(self):
        self.authenticated = False
        self.namespace = "dummy"
        self.calls = []

    async def authenticate_with_manual_grant(self, **kwargs):
        self.calls.append(kwargs)

    async def request(self, method, path, json_body=None, extra_headers=None):
        return SimpleNamespace(
            status_code=200, headers={}, text="{}", url="https://example.test"
        )


def test_perform_signed_fetch_forwards_timeout_to_manual_grant():
    async def run_test():
        client = DummyClient()
        result = await perform_signed_fetch(client, "GET", "/x", None, None, timeout=42)
        assert result["statusCode"] == 200
        assert client.calls == [
            {"on_verification_url": None, "grant_input": None, "timeout": 42}
        ]

    asyncio.run(run_test())

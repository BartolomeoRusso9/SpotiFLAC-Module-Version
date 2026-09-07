"""A 3xx is an answer, not a fault — and never worth retrying.

`_raise_for_status` used to fold every non-2xx into a `NetworkError`, which
the retryer treats as worth another go. So an unfollowed redirect cost three
identical requests and the backoff between them, to arrive three times at the
same `Location`. Songstats' ISRC lookup is the case that made this visible:
`songstats.com/<isrc>` answers `301 -> /track/<slug>/<title>`, so the redirect
*was* the data, and reading it as a network fault kept a whole fallback dead.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from SpotiFLAC.core.errors import NetworkError, RedirectNotFollowedError
from SpotiFLAC.core.http import AsyncHttpClient, _is_worth_retrying


def _redirect(status: int = 301, location: str = "https://example.invalid/real"):
    return httpx.Response(
        status,
        headers={"location": location} if location else {},
        request=httpx.Request("GET", "https://example.invalid/lookup"),
    )


# ---------------------------------------------------------------------------
# What a redirect raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_raises_its_own_error(status):
    with pytest.raises(RedirectNotFollowedError):
        AsyncHttpClient("t")._raise_for_status(_redirect(status))


def test_it_is_still_a_network_error_for_existing_handlers():
    """Callers catching NetworkError must not start missing these."""
    assert issubclass(RedirectNotFollowedError, NetworkError)


def test_the_message_names_where_the_server_pointed():
    """The `Location` is the whole content of a redirect, and the fix for it
    is always at the call site — so the message has to carry both."""
    with pytest.raises(RedirectNotFollowedError) as caught:
        AsyncHttpClient("t")._raise_for_status(
            _redirect(301, "https://songstats.com/track/vz1j72dw/bohemian-rhapsody"),
        )

    message = str(caught.value)
    assert "https://songstats.com/track/vz1j72dw/bohemian-rhapsody" in message
    assert "follow_redirects" in message
    assert caught.value.status == 301


def test_a_redirect_with_no_location_still_raises():
    with pytest.raises(RedirectNotFollowedError):
        AsyncHttpClient("t")._raise_for_status(_redirect(302, ""))


# ---------------------------------------------------------------------------
# What gets retried
# ---------------------------------------------------------------------------


def test_a_redirect_is_not_retried():
    """The server will say the same thing again. Three times over."""
    assert _is_worth_retrying(RedirectNotFollowedError("t", 301, "u", "v")) is False


def test_a_real_network_error_is_still_retried():
    assert _is_worth_retrying(NetworkError("t", "connection reset")) is True


def test_a_rate_limit_is_still_retried():
    from SpotiFLAC.core.errors import RateLimitedError

    assert _is_worth_retrying(RateLimitedError("t", 5)) is True


def test_an_unrelated_error_is_not_retried():
    assert _is_worth_retrying(ValueError("nope")) is False


def test_a_redirecting_endpoint_costs_exactly_one_request(monkeypatch):
    """The measured symptom: three requests where one was warranted."""
    calls = {"n": 0}

    class _Client:
        async def request(self, *args, **kwargs):
            calls["n"] += 1
            return _redirect(301)

    async def _fake_client():
        return _Client()

    client = AsyncHttpClient("t")
    monkeypatch.setattr(client, "_client", _fake_client)

    with pytest.raises(RedirectNotFollowedError):
        asyncio.run(client.get("https://example.invalid/lookup"))

    assert calls["n"] == 1


def test_a_transport_failure_still_gets_its_retries(monkeypatch):
    """The change must not have quietly disarmed retrying in general."""
    calls = {"n": 0}

    class _Client:
        async def request(self, *args, **kwargs):
            calls["n"] += 1
            raise httpx.ConnectError("boom")

    async def _fake_client():
        return _Client()

    client = AsyncHttpClient("t")
    monkeypatch.setattr(client, "_client", _fake_client)

    # DoH re-resolution is a different remedy and would confuse the count.
    async def _no_doh(*args, **kwargs):
        return None

    monkeypatch.setattr(client, "_retry_over_doh", _no_doh)
    monkeypatch.setattr(client, "_wait_strategy", lambda retry_state: 0)

    with pytest.raises(NetworkError):
        asyncio.run(client.get("https://example.invalid/x"))

    assert calls["n"] == 3

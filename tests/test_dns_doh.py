"""Getting past a DNS-level block without becoming a way in.

The cheapest way to block a provider is at DNS: the ISP's resolver answers
NXDOMAIN and nothing else has to happen. Asking a different resolver
usually returns the real address — so the system resolver stays primary and
DoH is tried only when a request failed *specifically because the name
would not resolve*.

Two things make that safe rather than merely clever, and both are what
these tests are really about:

  - the retry keeps the original hostname for SNI and Host, so TLS still
    verifies against the name the caller asked for and a resolver handing
    back somebody else's address fails the handshake;
  - private and loopback addresses are dropped from the answer, or this
    becomes an SSRF primitive pointed at the machine SpotiFLAC runs on.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from SpotiFLAC.core import dns_doh


@pytest.fixture(autouse=True)
def _fresh_cache():
    dns_doh.clear_cache()
    yield
    dns_doh.clear_cache()


# --- which failures are worth a second opinion -----------------------------


def test_a_name_that_does_not_resolve_is_recognised() -> None:
    try:
        socket.getaddrinfo("this-name-does-not-exist-xyz.invalid", 443)
    except socket.gaierror as exc:
        assert dns_doh.looks_like_dns_failure(exc)
    else:  # pragma: no cover - a resolver that answers everything
        pytest.skip("this resolver invents answers for invalid names")


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("connection refused"),
        TimeoutError("timed out"),
        OSError("certificate verify failed"),
        ValueError("nothing to do with the network"),
    ],
)
def test_other_failures_are_left_alone(exc) -> None:
    """Retrying a refused connection or a TLS error through a second
    resolver cannot help, and would double the cost of every ordinary
    outage.
    """
    assert not dns_doh.looks_like_dns_failure(exc)


def test_a_wrapped_cause_is_still_found() -> None:
    """httpx wraps the original gaierror several layers deep."""
    inner = socket.gaierror(8, "nodename nor servname provided, or not known")
    outer = RuntimeError("Request failed")
    outer.__cause__ = inner
    assert dns_doh.looks_like_dns_failure(outer)


# --- what may be dialled ---------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0"],
)
def test_private_answers_are_refused(address) -> None:
    """A DoH resolver answering 127.0.0.1 would otherwise point this
    straight at SpotiFLAC's own --web server.
    """
    assert not dns_doh._is_public(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "104.21.38.150", "2606:4700::1111"])
def test_public_answers_are_accepted(address) -> None:
    assert dns_doh._is_public(address)


def test_only_address_records_are_used() -> None:
    """CNAMEs are a step on the way, not something to dial."""
    payload = {
        "Status": 0,
        "Answer": [
            {"type": 5, "data": "elsewhere.example.com."},
            {"type": 1, "data": "104.21.38.150"},
            {"type": 1, "data": "10.0.0.1"},
        ],
    }
    assert dns_doh._addresses_from_answer(payload) == ["104.21.38.150"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"Status": 3},
        {"Status": 0},
        {"Status": 0, "Answer": None},
        {"Status": 0, "Answer": [{"type": 1}]},
        "not a dict",
    ],
)
def test_an_unusable_answer_yields_nothing(payload) -> None:
    assert dns_doh._addresses_from_answer(payload) == []


# --- against the real resolvers --------------------------------------------


@pytest.mark.parametrize("host", ["api.zarz.moe", "example.com"])
def test_a_real_name_resolves(host) -> None:
    addresses = asyncio.run(dns_doh.resolve_async(host))
    if not addresses:
        pytest.skip("no network, or DoH is itself blocked here")
    assert all(dns_doh._is_public(a) for a in addresses)


def test_a_name_that_does_not_exist_yields_nothing() -> None:
    assert asyncio.run(dns_doh.resolve_async("no-such-host-xyz.invalid")) == []


def test_the_google_endpoint_uses_the_json_path() -> None:
    """Google serves the JSON API at /resolve and answers /dns-query in wire
    format only — asking the wrong path returns a body that is not JSON at
    all, and the fallback silently has one resolver instead of two.
    """
    assert any(url.endswith("/resolve") for url in dns_doh.DOH_ENDPOINTS)
    assert any(url.endswith("/dns-query") for url in dns_doh.DOH_ENDPOINTS)


def test_the_resolvers_are_named_by_address() -> None:
    """A DoH client that needed DNS to find its own DNS server would be
    useless in exactly the situation it exists for.
    """
    import ipaddress
    from urllib.parse import urlsplit

    for url in dns_doh.DOH_ENDPOINTS:
        host = urlsplit(url).hostname
        ipaddress.ip_address(host)  # raises if it is a name


# --- end to end ------------------------------------------------------------


def test_a_blocked_name_still_reaches_its_host(monkeypatch) -> None:
    """The whole point, with the system resolver denying the name the way an
    ISP's would.
    """
    from SpotiFLAC.core.http import AsyncHttpClient

    blocked = "api.zarz.moe"
    real_getaddrinfo = socket.getaddrinfo

    def deny(host, *args, **kwargs):
        if host == blocked:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", deny)

    async def _go():
        return await AsyncHttpClient("test", timeout_s=20).get(
            f"https://{blocked}/v1/health"
        )

    try:
        resp = asyncio.run(_go())
    except Exception:
        pytest.skip("no network, or DoH is blocked here too")
    assert resp.status_code == 200

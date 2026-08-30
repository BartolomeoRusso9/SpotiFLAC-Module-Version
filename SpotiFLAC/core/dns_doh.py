"""SpotiFLAC/core/dns_doh.py — a second opinion when the resolver says no.

The simplest way to block a provider is at DNS: the ISP's resolver answers
NXDOMAIN for the host and nothing else has to happen. It is also the easiest
to work around, because the block is in the answer rather than in the route
— asking a resolver that is not the ISP's usually returns the real address.

So: the system resolver stays the primary path and nothing changes for
anyone unaffected. Only when a request fails *specifically because the name
would not resolve* is the host looked up over DNS-over-HTTPS and dialled by
address.

Two properties make this safe rather than merely clever:

  - TLS still verifies against the original hostname (via SNI and the Host
    header, set by the caller), so a resolver returning an attacker's
    address cannot silently redirect traffic — the handshake fails instead.
  - Private and loopback addresses are dropped from the answer. A DoH
    resolver that returned 127.0.0.1 would otherwise turn this into an SSRF
    primitive against the machine SpotiFLAC runs on.

Ported from the mobile app's dns_doh.go, including the choice of upstreams.
Their URLs are literal IPs on purpose: a DoH client that needed DNS to find
its DNS server would be useless in exactly the situation it exists for.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time

logger = logging.getLogger(__name__)

#: Resolvers, by literal address so this never needs DNS itself. Cloudflare
#: serves the JSON API at /dns-query; Google serves it at /resolve and
#: answers /dns-query only in wire format — a detail worth pinning down,
#: since asking the wrong path returns a body that is not JSON at all.
DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://8.8.8.8/resolve",
)

#: How long a successful answer is reused. Short: this exists to get past a
#: block, not to become a resolver.
_TTL_S = 300.0

_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = asyncio.Lock()


def _is_public(address: str) -> bool:
    """Whether an address is one we are willing to dial.

    Anything private, loopback, link-local or unspecified is refused: a DoH
    answer pointing at the local network is either a misconfiguration or an
    attempt to make this an SSRF primitive, and neither is worth following.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    )


def _addresses_from_answer(payload: object) -> list[str]:
    """The A/AAAA records in a DoH JSON response, public ones only."""
    if not isinstance(payload, dict) or payload.get("Status") != 0:
        return []
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return []
    found: list[str] = []
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        # 1 = A, 28 = AAAA. Anything else (CNAME, and so on) is a step on
        # the way rather than an address to dial.
        if answer.get("type") not in (1, 28):
            continue
        address = str(answer.get("data") or "").strip()
        if address and _is_public(address):
            found.append(address)
    return found


async def resolve_async(hostname: str, *, timeout_s: float = 8.0) -> list[str]:
    """Addresses for `hostname` over DoH, best first. [] if none can be had.

    Never raises: every caller is already handling a failure, and turning
    one failure into a different exception helps nobody.
    """
    host = (hostname or "").strip().rstrip(".")
    if not host:
        return []

    async with _cache_lock:
        cached = _cache.get(host)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])

    from .http import NetworkManager

    for endpoint in DOH_ENDPOINTS:
        try:
            client = await NetworkManager.get_async_client_safe()
            resp = await client.get(
                endpoint,
                params={"name": host, "type": "A"},
                headers={"Accept": "application/dns-json"},
                timeout=timeout_s,
            )
            if resp.status_code != 200:
                continue
            addresses = _addresses_from_answer(resp.json())
        except Exception as exc:
            logger.debug("[doh] %s failed for %s: %s", endpoint, host, exc)
            continue

        if addresses:
            async with _cache_lock:
                _cache[host] = (time.monotonic() + _TTL_S, list(addresses))
            logger.debug("[doh] %s resolved to %s via %s", host, addresses, endpoint)
            return addresses

    logger.debug("[doh] no usable address for %s", host)
    return []


def looks_like_dns_failure(exc: BaseException) -> bool:
    """Whether an exception means "that name does not resolve".

    Deliberately narrow. A connection refused, a timeout or a TLS error are
    all reasons a request failed that DoH cannot help with, and retrying
    those through a second resolver would double the cost of every ordinary
    outage.
    """
    seen: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < 5:
        seen.append(f"{type(current).__name__}: {current}".lower())
        current = current.__cause__ or current.__context__

    joined = " | ".join(seen)
    markers = (
        "name or service not known",
        "nodename nor servname",
        "temporary failure in name resolution",
        "no address associated with hostname",
        "getaddrinfo failed",
        "name does not resolve",
        "[errno 8]",  # macOS EAI_NONAME
        "[errno -2]",  # Linux EAI_NONAME
        "[errno -3]",  # Linux EAI_AGAIN
        "[errno 11001]",  # Windows WSAHOST_NOT_FOUND
    )
    return any(marker in joined for marker in markers)


def clear_cache() -> None:
    """Forgets every resolved address. For tests."""
    _cache.clear()

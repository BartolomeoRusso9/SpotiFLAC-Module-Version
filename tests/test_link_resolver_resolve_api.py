"""Cross-platform links have to come from something that answers.

Odesli retired free public access to Songlink's v1-alpha.1 API, and every
request now returns 401 PUBLIC_API_ACCESS_DEPRECATED — measured directly,
with and without our own headers, for both the `url` and `isrc` forms. The
resolver kept asking it and kept getting nothing, silently: callers just saw
an empty link map.

The mobile app already routes Spotify URLs through a resolve endpoint on the
project's own infrastructure and uses Songlink only as a fallback. These
tests pin that order down, and pin down the response shapes the endpoint
actually returns — its values are sometimes a bare URL string and sometimes
an object wrapping one.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.link_resolver import LinkResolver

RESOLVED = {
    "success": True,
    "isrc": "USUM70504267",
    "songUrls": {
        "Spotify": "https://open.spotify.com/track/0NJu93oln1kkgbHLFzLJ4h",
        "Deezer": {"url": "https://www.deezer.com/track/634430472"},
        "Tidal": None,
    },
}


def _resolver(monkeypatch, *, resolve=None, songlink=None) -> LinkResolver:
    resolver = LinkResolver()
    calls: list[str] = []

    async def fake_resolve(payload):
        calls.append("resolve")
        return resolve or {}

    async def fake_songlink(params):
        calls.append("songlink")
        return songlink or {}

    monkeypatch.setattr(resolver, "_resolve_links_async", fake_resolve)
    monkeypatch.setattr(resolver, "_get_songlink_links_async", fake_songlink)
    resolver.calls = calls  # type: ignore[attr-defined]
    return resolver


def test_the_resolve_api_is_asked_first(monkeypatch) -> None:
    resolver = _resolver(monkeypatch, resolve={"spotify": "https://x"})
    links = asyncio.run(resolver._get_songlink_links_by_url_async("https://y"))
    assert links == {"spotify": "https://x"}
    assert resolver.calls == ["resolve"], "Songlink must not be asked needlessly"


def test_songlink_is_still_tried_when_resolve_returns_nothing(monkeypatch) -> None:
    """Kept rather than deleted: the day Odesli access returns, finding out
    costs one request.
    """
    resolver = _resolver(monkeypatch, resolve={}, songlink={"deezer": "https://d"})
    links = asyncio.run(resolver._get_songlink_links_by_url_async("https://y"))
    assert links == {"deezer": "https://d"}
    assert resolver.calls == ["resolve", "songlink"]


def test_the_id_based_lookup_uses_the_same_order(monkeypatch) -> None:
    resolver = _resolver(monkeypatch, resolve={"tidal": "https://t"})
    links = asyncio.run(resolver._get_songlink_links_by_id_async("abc", "spotify"))
    assert links == {"tidal": "https://t"}
    assert resolver.calls == ["resolve"]


# --- reading the endpoint's answer -----------------------------------------


def test_both_value_shapes_are_accepted() -> None:
    """A bare URL string for one platform and an object for the next, in the
    same response — assuming either shape drops half the links.
    """
    links = LinkResolver()._process_resolve_response(RESOLVED)
    assert links["spotify"].startswith("https://open.spotify.com/")
    assert links["deezer"].startswith("https://www.deezer.com/")


def test_a_null_platform_is_skipped_not_recorded_as_empty() -> None:
    assert "tidal" not in LinkResolver()._process_resolve_response(RESOLVED)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"success": False, "songUrls": {"Spotify": "https://x"}},
        {"success": True},
        {"success": True, "songUrls": None},
        {"success": True, "songUrls": []},
        None,
        "not a dict",
    ],
)
def test_a_failed_or_malformed_answer_yields_no_links(payload) -> None:
    """Anything unusable has to fall through to the Songlink attempt rather
    than propagate as an error.
    """
    assert LinkResolver()._process_resolve_response(payload) == {}


def test_a_transport_failure_is_not_an_exception(monkeypatch) -> None:
    resolver = LinkResolver()

    class _Boom:
        async def post(self, *a, **k):
            raise OSError("connection reset")

    monkeypatch.setattr(resolver, "http", _Boom())
    assert asyncio.run(resolver._resolve_links_async({"url": "https://x"})) == {}

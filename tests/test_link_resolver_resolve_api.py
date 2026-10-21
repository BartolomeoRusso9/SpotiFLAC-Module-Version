"""Cross-platform link resolution through the active resolver API.

The resolver is intentionally tested without a live service here. The real
endpoint is exercised separately during manual diagnostics; these tests pin
response normalization, fallback ordering, and malformed-response handling.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest

from mornye.core.link_resolver import LinkResolver

RESOLVED = {
    "success": True,
    "isrc": "USUM70504267",
    "songUrls": {
        "Spotify": "https://open.spotify.com/track/0NJu93oln1kkgbHLFzLJ4h",
        "Deezer": {"url": "https://www.deezer.com/track/634430472"},
        "Tidal": None,
    },
}


def _resolver(monkeypatch, *, resolve=None) -> LinkResolver:
    """Create a resolver stub that records payloads and returns configured links."""
    resolver = LinkResolver()
    calls: list[dict] = []

    async def fake_resolve(payload):
        """Record a resolver payload and return the configured response."""
        calls.append(payload)
        return resolve or {}

    monkeypatch.setattr(resolver, "_resolve_links_async", fake_resolve)
    cast(Any, resolver).calls = calls
    return resolver


def test_the_resolve_api_is_used_for_a_source_url(monkeypatch) -> None:
    """Verify URL resolution forwards the source URL to the resolver API."""
    resolver = _resolver(monkeypatch, resolve={"spotify": "https://spotify"})
    links = asyncio.run(resolver._get_resolve_links_by_url_async("https://source"))
    assert links == {"spotify": "https://spotify"}
    assert cast(Any, resolver).calls == [{"url": "https://source"}]


def test_the_resolve_api_is_used_for_a_platform_id(monkeypatch) -> None:
    """Verify ID resolution sends the platform, track type, and raw ID."""
    resolver = _resolver(monkeypatch, resolve={"tidal": "https://tidal"})
    links = asyncio.run(resolver._get_resolve_links_by_id_async("abc", "spotify"))
    assert links == {"tidal": "https://tidal"}
    assert cast(Any, resolver).calls == [
        {"platform": "spotify", "type": "track", "id": "abc"}
    ]


def test_an_isrc_does_not_replace_the_source_id_lookup(monkeypatch) -> None:
    """The source edition is authoritative; ISRC lookups only fill gaps."""
    resolver = LinkResolver()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "mornye.core.link_resolver.get_cached_response",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "mornye.core.link_resolver.put_cached_response",
        lambda *_args, **_kwargs: None,
    )

    async def deezer_by_isrc(_isrc):
        """Simulate an ISRC lookup matching a different Deezer edition."""
        return "https://www.deezer.com/track/reissue"

    async def by_id(raw_id, platform):
        """Record the source lookup and return links for the original edition."""
        calls.append((raw_id, platform))
        return {
            "spotify": "https://open.spotify.com/track/source",
            "appleMusic": "https://music.apple.com/track/source",
        }

    async def by_url(_url):
        """Return no extra links for the Deezer reissue URL."""
        return {}

    async def empty(*_args):
        """Return no Songstats fallback links."""
        return {}

    monkeypatch.setattr(resolver, "_get_deezer_url_by_isrc_async", deezer_by_isrc)
    monkeypatch.setattr(resolver, "_get_resolve_links_by_id_async", by_id)
    monkeypatch.setattr(resolver, "_get_resolve_links_by_url_async", by_url)
    monkeypatch.setattr(resolver, "_get_songstats_links_async", empty)

    links = asyncio.run(
        resolver.resolve_all_async("spotify_source_with_isrc", "USUM70504267")
    )

    assert calls == [("source_with_isrc", "spotify")]
    assert links["spotify"] == "https://open.spotify.com/track/source"
    assert links["appleMusic"] == "https://music.apple.com/track/source"
    assert links["deezer"] == "https://www.deezer.com/track/reissue"


def test_both_value_shapes_are_accepted() -> None:
    """Verify resolver links accept plain URL strings and URL dictionaries."""
    links = LinkResolver()._process_resolve_response(RESOLVED)
    assert links["spotify"].startswith("https://open.spotify.com/")
    assert links["deezer"].startswith("https://www.deezer.com/")


def test_unitune_response_maps_platform_links() -> None:
    payload = {
        "linksByPlatform": {
            "spotify": {"url": "https://open.spotify.com/track/abc"},
            "deezer": {"url": "https://deezer.com/us/track/123"},
            "appleMusic": {"url": "https://music.apple.com/us/song/title/456"},
        },
    }

    assert LinkResolver()._process_unitune_response(payload) == {
        "spotify": "https://open.spotify.com/track/abc",
        "deezer": "https://www.deezer.com/track/123",
        "appleMusic": "https://music.apple.com/us/song/title/456",
    }


def test_unitune_is_tried_when_primary_resolver_has_no_links() -> None:
    resolver = LinkResolver()
    calls: list[dict] = []

    class _Client:
        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={"success": False},
                request=httpx.Request("POST", "https://api.zarz.moe/v1/resolve"),
            )

        async def get(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return httpx.Response(
                200,
                json={
                    "linksByPlatform": {
                        "deezer": {"url": "https://deezer.com/track/123"}
                    }
                },
                request=httpx.Request("GET", url),
            )

    resolver.http = _Client()
    links = asyncio.run(
        resolver._resolve_links_async(
            {"platform": "spotify", "type": "track", "id": "track id"}
        )
    )

    assert links == {"deezer": "https://www.deezer.com/track/123"}
    assert calls[0]["params"] == {
        "url": "https://open.spotify.com/track/track%20id",
    }


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
    """Verify unsuccessful or malformed resolver payloads produce no links."""
    assert LinkResolver()._process_resolve_response(payload) == {}


def test_a_transport_failure_is_not_an_exception(monkeypatch) -> None:
    resolver = LinkResolver()

    class _Boom:
        async def post(self, *a, **k):
            raise OSError("connection reset")

    monkeypatch.setattr(resolver, "http", _Boom())
    assert asyncio.run(resolver._resolve_links_async({"url": "https://x"})) == {}


def test_an_isrc_resolves_to_spotify_through_the_resolve_api(monkeypatch) -> None:
    """Verify normalized ISRC lookup resolves a Deezer match to a Spotify URL."""
    resolver = LinkResolver()
    seen: list[dict] = []

    async def fake_deezer(isrc):
        assert isrc == "USUM70504267"
        return "https://www.deezer.com/track/634430472"

    async def fake_resolve(payload):
        """Record the Deezer lookup payload and return its Spotify match."""
        seen.append(payload)
        return {"spotify": "https://open.spotify.com/track/0NJu93oln1kkgbHLFzLJ4h"}

    monkeypatch.setattr(resolver, "_get_deezer_url_by_isrc_async", fake_deezer)
    monkeypatch.setattr(resolver, "_resolve_links_async", fake_resolve)

    url = asyncio.run(resolver.spotify_url_for_isrc_async("usum70504267"))
    assert url == "https://open.spotify.com/track/0NJu93oln1kkgbHLFzLJ4h"
    assert seen == [{"url": "https://www.deezer.com/track/634430472"}]


def test_an_isrc_without_a_deezer_match_stays_empty(monkeypatch) -> None:
    """Verify blank ISRCs and missing Deezer matches return an empty URL."""
    resolver = LinkResolver()

    async def no_deezer(_isrc):
        """Simulate an ISRC with no matching Deezer track."""
        return ""

    monkeypatch.setattr(resolver, "_get_deezer_url_by_isrc_async", no_deezer)
    assert asyncio.run(resolver.spotify_url_for_isrc_async("USUM70504267")) == ""
    assert asyncio.run(resolver.spotify_url_for_isrc_async("  ")) == ""

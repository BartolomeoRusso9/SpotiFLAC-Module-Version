"""Songstats, and the three ways reading it went wrong.

The lookup `songstats.com/<isrc>` is not a page — it is a redirect to
`songstats.com/track/<slug>/<title>`, where the data is. That single fact is
behind all of this: the 301 was being treated as a failure, retried three
times and swallowed, so the fallback had never returned a link in its life.

The fixture below is the shape of the real page as of 2026-09-06: one
JSON-LD graph carrying an Organization (whose `sameAs` is social media) and
a MusicRecording (whose `sameAs` lists artist, album *and* track links for
each platform, artist first). That ordering is the whole of the second bug.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from SpotiFLAC.core.link_resolver import LinkResolver

#: Trimmed from the live page for ISRC GBUM71029604 (Bohemian Rhapsody).
#: The artist links come first, exactly as songstats orders them.
SONGSTATS_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Organization","name":"Songstats",
  "sameAs":["https://www.instagram.com/songstats","https://x.com/songstatsapp"]},
 {"@type":"MusicRecording","name":"Bohemian Rhapsody","isrcCode":"GBUM71029604",
  "sameAs":[
   "https://listen.tidal.com/artist/8992/",
   "https://music.amazon.com/artists/B000QK71AG?ref=dm_ff_tracklists",
   "https://deezer.com/us/artist/412",
   "https://listen.tidal.com/track/37091477",
   "https://music.amazon.com/albums/B0157E3W76/B0157E47K2?ref=dm_ff_tracklists",
   "https://deezer.com/us/track/9997018"]}]}
</script>
"""


# ---------------------------------------------------------------------------
# The redirect is the answer
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Stands in for the shared client, and remembers how it was called."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._response


def _response(status: int = 200, text: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        text=text,
        request=httpx.Request("GET", "https://songstats.com/GBUM71029604"),
    )


def test_the_scrape_follows_redirects():
    """`songstats.com/<isrc>` is a 301 to the page. Not following it is not
    a failure to handle — it is failing to make the request at all."""
    client = _RecordingClient(_response(text=SONGSTATS_HTML))
    resolver = LinkResolver(client)

    asyncio.run(resolver._safe_get_html("https://songstats.com/GBUM71029604"))

    assert client.calls, "no request was made"
    assert client.calls[0]["follow_redirects"] is True


def test_a_songstats_lookup_costs_one_request():
    """Three identical requests and 3.6s of backoff was the old behaviour."""
    client = _RecordingClient(_response(text=SONGSTATS_HTML))
    resolver = LinkResolver(client)

    links = asyncio.run(resolver._get_songstats_links_async("GBUM71029604"))

    assert len(client.calls) == 1
    assert links, "the page carries links; the lookup returned none"


# ---------------------------------------------------------------------------
# Only the track link, for every platform
# ---------------------------------------------------------------------------


def test_the_track_link_is_taken_not_the_artist_link():
    """`sameAs` lists the artist first, and first-match took it.

    Handing an artist URL back as a track is worse than returning nothing:
    nothing is a signal the caller can act on, and a plausible wrong link
    is not.
    """
    links = LinkResolver()._process_songstats_links(SONGSTATS_HTML)

    assert links["tidal"] == "https://listen.tidal.com/track/37091477"
    assert links["deezer"] == "https://www.deezer.com/track/9997018"
    # `/albums/<album asin>/<track asin>` is one of the three ways Amazon
    # spells a track, and the normaliser rewrites it to the canonical one.
    assert links["amazonMusic"] == (
        "https://music.amazon.com/tracks/B0157E47K2?musicTerritory=US"
    )


@pytest.mark.parametrize(
    "link",
    [
        "https://listen.tidal.com/artist/8992/",
        "https://music.amazon.com/artists/B000QK71AG",
        "https://deezer.com/us/artist/412",
        # An album is not a track either.
        "https://music.amazon.com/albums/B0157E3W76",
    ],
)
def test_a_page_of_only_artist_links_yields_nothing(link):
    results: dict[str, str] = {"amazonMusic": "", "tidal": "", "deezer": ""}

    LinkResolver()._assign_songstats_link(link, results)

    assert not any(results.values()), f"{link} was taken for a track"


def test_the_canonical_amazon_track_shape_still_works():
    """The shape the old test used — it must not be a casualty of the fix."""
    results: dict[str, str] = {"amazonMusic": "", "tidal": "", "deezer": ""}

    LinkResolver()._assign_songstats_link(
        "https://music.amazon.com/tracks/B07T2G5CB2?musicTerritory=US",
        results,
    )

    assert results["amazonMusic"] == (
        "https://music.amazon.com/tracks/B07T2G5CB2?musicTerritory=US"
    )


# ---------------------------------------------------------------------------
# The ISRC chain
# ---------------------------------------------------------------------------


def test_songstats_is_no_longer_asked_for_an_isrc():
    """It was asked to turn a Spotify id into an ISRC and never could.

    Its lookup is keyed by the ISRC itself, and the one route that takes a
    Spotify id serves a client-rendered shell with no ISRC in the HTML. The
    call was a request per track for a guaranteed None.
    """
    from SpotiFLAC.core import isrc_helper

    helper = isrc_helper.IsrcHelper(None)

    assert not hasattr(helper, "songstats")


def test_the_dead_module_is_gone():
    with pytest.raises(ImportError):
        __import__("SpotiFLAC.core.songstats")

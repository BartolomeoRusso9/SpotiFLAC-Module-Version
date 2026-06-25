import unittest
import asyncio
from unittest.mock import Mock
from unittest.mock import AsyncMock

from SpotiFLAC.core.link_resolver import LinkResolver


class LinkResolverTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.http.get_json_async = AsyncMock()
        self.http.get_async = AsyncMock()
        self.http.get = Mock()
        self.resolver = LinkResolver(self.http)

    def test_process_songlink_response_normalizes_platforms(self):
        data = {
            "linksByPlatform": {
                "deezer": {"url": "https://www.deezer.com/track/123"},
                "amazonMusic": {"url": "https://music.amazon.com/tracks/B123456789?musicTerritory=US"},
                "appleMusic": {"url": "https://music.apple.com/track/123"},
                "spotify": {"url": "https://open.spotify.com/track/abc"},
            }
        }
        links = self.resolver._process_songlink_response(data)

        self.assertEqual(links["deezer"], "https://www.deezer.com/track/123")
        self.assertEqual(links["amazonMusic"], "https://music.amazon.com/tracks/B123456789?musicTerritory=US")
        self.assertEqual(links["appleMusic"], "https://music.apple.com/track/123")
        self.assertEqual(links["spotify"], "https://open.spotify.com/track/abc")

    def test_resolve_all_uses_songlink_without_double_encoding(self):
        self.http.get_json_async.side_effect = [
            {"link": "https://www.deezer.com/track/123", "id": 123},
            {"linksByPlatform": {"amazonMusic": {"url": "https://music.amazon.com/tracks/B123456789?musicTerritory=US"}}},
        ]
        self.http.get.return_value = Mock(text="")

        links = asyncio.run(self.resolver.resolve_all_async("spotify_ABCDEFGHIJKLMN", isrc="USRC17607839"))

        self.assertEqual(links["amazonMusic"], "https://music.amazon.com/tracks/B123456789?musicTerritory=US")

    def test_get_songlink_html_links_parses_platform_urls(self):
        html = (
            "<html>"
            "<a href=\"https://www.deezer.com/track/123\"></a>"
            "<script>trackAsin=B123456789</script>"
            "<a href=\"https://listen.tidal.com/track/56789\"></a>"
            "</html>"
        )
        self.http.get_async.return_value = Mock(text=html)

        links = asyncio.run(self.resolver._get_songlink_html_links_async("ABCDEFG"))

        self.assertEqual(links["deezer"], "https://www.deezer.com/track/123")
        self.assertEqual(links["amazonMusic"], "https://music.amazon.com/tracks/B123456789?musicTerritory=US")
        self.assertEqual(links["tidal"], "https://listen.tidal.com/track/56789")


if __name__ == "__main__":
    unittest.main()

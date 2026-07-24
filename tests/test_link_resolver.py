import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from SpotiFLAC.core.link_resolver import LinkResolver


class LinkResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.http = Mock()
        self.http.get_json_async = AsyncMock()
        self.http.get_async = AsyncMock()
        self.http.get = Mock()
        self.resolver = LinkResolver(self.http)

    def test_process_songlink_response_normalizes_platforms(self) -> None:
        data = {
            "linksByPlatform": {
                "deezer": {"url": "https://www.deezer.com/track/123"},
                "amazonMusic": {
                    "url": "https://music.amazon.com/tracks/B123456789?musicTerritory=US",
                },
                "appleMusic": {"url": "https://music.apple.com/track/123"},
                "spotify": {"url": "https://open.spotify.com/track/abc"},
            },
        }
        links = self.resolver._process_songlink_response(data)

        assert links["deezer"] == "https://www.deezer.com/track/123"
        assert (
            links["amazonMusic"]
            == "https://music.amazon.com/tracks/B123456789?musicTerritory=US"
        )
        assert links["appleMusic"] == "https://music.apple.com/track/123"
        assert links["spotify"] == "https://open.spotify.com/track/abc"

    def test_resolve_all_uses_songlink_without_double_encoding(self) -> None:
        self.http.get_json_async.side_effect = [
            {"link": "https://www.deezer.com/track/123", "id": 123},
            {
                "linksByPlatform": {
                    "amazonMusic": {
                        "url": "https://music.amazon.com/tracks/B123456789?musicTerritory=US",
                    },
                },
            },
        ]
        self.http.get.return_value = Mock(text="")

        links = asyncio.run(
            self.resolver.resolve_all_async(
                "spotify_ABCDEFGHIJKLMN",
                isrc="USRC17607839",
            ),
        )

        assert (
            links["amazonMusic"]
            == "https://music.amazon.com/tracks/B123456789?musicTerritory=US"
        )

    def test_get_songlink_html_links_parses_platform_urls(self) -> None:
        html = (
            "<html>"
            '<a href="https://www.deezer.com/track/123"></a>'
            "<script>trackAsin=B123456789</script>"
            '<a href="https://listen.tidal.com/track/56789"></a>'
            "</html>"
        )
        self.http.get_async.return_value = Mock(text=html)

        links = asyncio.run(self.resolver._get_songlink_html_links_async("ABCDEFG"))

        assert links["deezer"] == "https://www.deezer.com/track/123"
        assert (
            links["amazonMusic"]
            == "https://music.amazon.com/tracks/B123456789?musicTerritory=US"
        )
        assert links["tidal"] == "https://listen.tidal.com/track/56789"

    def test_get_songlink_links_by_id_passes_type_song(self) -> None:
        self.http.get_json_async.return_value = {}

        asyncio.run(self.resolver._get_songlink_links_by_id_async("abc", "spotify"))

        _, kwargs = self.http.get_json_async.call_args
        assert kwargs["params"]["type"] == "song"


if __name__ == "__main__":
    unittest.main()

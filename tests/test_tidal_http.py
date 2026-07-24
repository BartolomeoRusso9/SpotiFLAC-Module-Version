import asyncio

from SpotiFLAC.providers import tidal


def test_search_on_mirrors_finds_track(mock_network_client) -> None:
    # prepare a fake search response containing a matching track
    # Simplified payload format with top-level items list
    fake = {
        "items": [
            {
                "id": 12345,
                "title": "Test Track",
                "artists": [{"name": "Artist"}],
                "duration": 180,
            },
        ],
    }
    mock_network_client([("/search", 200, fake)])

    provider = tidal.TidalProvider(apis=["https://api.test"])
    res = asyncio.run(
        provider._search_on_mirrors_async("Test Track", "Artist", "", 180),
    )
    assert res is not None
    assert "listen.tidal.com/track/" in res


def test_fetch_tidal_url_once_handles_rate_limit_and_success(
    mock_network_client,
) -> None:
    # First call returns 429 then success with manifest
    manifest_payload = {"manifest": "BASE64DATA"}
    seq = [
        ("/track/", 429, {"error": "rate limited"}),
        ("/track/", 200, manifest_payload),
    ]
    mock_network_client(seq)

    api = "https://api.test"
    url = asyncio.run(
        tidal._fetch_tidal_url_once_async(
            api,
            track_id=1,
            quality="LOSSLESS",
            timeout_s=2,
        ),
    )
    assert url is not None

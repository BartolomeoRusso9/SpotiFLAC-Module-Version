import asyncio

import pytest

from mornye.core.models import TrackMetadata
from mornye.extensions import python_provider


def _metadata() -> TrackMetadata:
    """Build Spotify metadata for testing Python provider ID adaptation."""
    return TrackMetadata(
        id="0JYt03Y1nb8wHWxPBkVncG",
        title="Example",
        artists="Artist",
        album="Album",
        album_artist="Artist",
        external_url="https://open.spotify.com/track/0JYt03Y1nb8wHWxPBkVncG",
    )


def test_native_ids_are_extracted_from_provider_links() -> None:
    """Verify numeric track IDs and Amazon ASINs are extracted from provider URLs."""
    assert (
        python_provider._native_track_id("tidal", "https://tidal.com/browse/track/123")
        == "123"
    )
    assert (
        python_provider._native_track_id("qobuz", "https://open.qobuz.com/track/456")
        == "456"
    )
    assert (
        python_provider._native_track_id(
            "amazon", "https://music.amazon.com/tracks/B012345678"
        )
        == "B012345678"
    )


def test_python_hint_adapts_tidal_id_without_editing_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Tidal ID adaptation preserves the original Spotify external URL."""

    class FakeResolver:
        async def resolve_provider_url_async(
            self, source_url: str, provider: str
        ) -> str:
            """Require a Tidal lookup and return a deterministic native track URL."""
            assert provider == "tidal"
            return "https://tidal.com/browse/track/123"

    monkeypatch.setattr(python_provider, "LinkResolver", FakeResolver)
    fake_provider = type("Provider", (), {"name": "tidal"})()

    adapted = asyncio.run(
        python_provider._metadata_with_provider_hint(fake_provider, _metadata())
    )

    assert adapted.id == "tidal_123"
    assert adapted.external_url == _metadata().external_url

"""Python-only Spotify-to-provider hints for JavaScript extensions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mornye.core.models import TrackMetadata
from mornye.extensions.provider import JSExtensionProvider

SPOTIFY_URL = "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"


def _provider(name: str) -> JSExtensionProvider:
    """Build a minimal JavaScript provider without loading an extension runtime."""
    provider = object.__new__(JSExtensionProvider)
    provider._ext = SimpleNamespace(name=name)
    provider.name = f"ext:{name}"
    return provider


def _metadata(url: str = SPOTIFY_URL) -> TrackMetadata:
    """Build Spotify track metadata with a configurable external URL."""
    return TrackMetadata(
        id="0VjIjW4GlUZAMYd2vXMi3b",
        title="Blinding Lights",
        artists="The Weeknd",
        album="After Hours",
        album_artist="The Weeknd",
        isrc="USUG11904257",
        duration_ms=200000,
        external_url=url,
    )


@pytest.mark.parametrize(
    ("extension", "target_url", "expected_key", "expected_value"),
    [
        (
            "tidal-web",
            "https://tidal.com/browse/track/134858527",
            "tidal_id",
            "134858527",
        ),
        (
            "qobuz-web",
            "https://open.qobuz.com/track/101686517",
            "qobuz_id",
            "101686517",
        ),
        (
            "deezer",
            "https://www.deezer.com/track/908604612",
            "deezer_id",
            "908604612",
        ),
        (
            "amazon",
            "https://music.amazon.com/tracks/B086R4RN3S?musicTerritory=US",
            "amazon_id",
            "B086R4RN3S",
        ),
    ],
)
def test_provider_gets_native_id_from_spotify_link(
    monkeypatch,
    extension: str,
    target_url: str,
    expected_key: str,
    expected_value: str,
) -> None:
    """Verify Spotify links produce native provider hints for availability checks."""

    async def resolve(self, source_url: str, provider: str) -> str:
        """Validate the source and provider before returning the native track URL."""
        assert source_url == SPOTIFY_URL
        assert provider == extension.removesuffix("-web")
        return target_url

    monkeypatch.setattr(
        "mornye.extensions.provider.LinkResolver.resolve_provider_url_async",
        resolve,
    )

    options = asyncio.run(_provider(extension)._provider_match_options(_metadata()))

    assert options["spotify_url"] == SPOTIFY_URL
    assert options[expected_key] == expected_value
    if extension == "amazon":
        # Amazon's existing JS contract recognizes an ASIN in spotify_id.
        assert options["spotify_id"] == expected_value


def test_foreign_provider_metadata_is_not_fabricated_as_spotify(monkeypatch) -> None:
    """Verify foreign IDs and URLs do not trigger Spotify-based provider hints."""

    async def fail(*args, **kwargs):
        """Fail if foreign metadata unexpectedly invokes the Spotify link resolver."""
        raise AssertionError("resolver must not be called")

    monkeypatch.setattr(
        "mornye.extensions.provider.LinkResolver.resolve_provider_url_async",
        fail,
    )
    metadata = _metadata("https://music.apple.com/us/song/example/123")
    metadata.id = "apple_123"

    options = asyncio.run(_provider("qobuz-web")._provider_match_options(metadata))

    assert "spotify_url" not in options
    assert "qobuz_id" not in options

"""`core/metadata_fallback.py` — an extension, when the built-in client fails.

The order is the point: the built-in Spotify and Apple Music clients are
always asked first, and an extension only when they raise or come back
empty. What must not happen is the opposite — an extension consulted while
the built-in client works, a malformed link sent around to a second reader,
or the built-in client's real error replaced by the fallback's.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import SpotiFLAC.downloader as downloader_module
from SpotiFLAC.core.errors import ErrorKind, SpotiflacError
from SpotiFLAC.core.metadata_fallback import (
    fallback_extension_for,
    get_url_with_fallback,
    track_from_extension,
)
from SpotiFLAC.extensions.manager import InstalledExtension

SPOTIFY_ALBUM = "https://open.spotify.com/album/5V8n6fqyAPxvFTibPhQVcp"
APPLE_ALBUM = "https://music.apple.com/us/album/palette/1229073300"


def _installed(name, types, patterns, enabled=True):
    return InstalledExtension(
        name=name,
        display_name=name,
        version="1.0.0",
        description="",
        ext_dir=Path("/nonexistent") / name,
        manifest={
            "name": name,
            "type": list(types),
            "urlHandler": {"enabled": enabled, "patterns": list(patterns)},
        },
    )


class _Manager:
    def __init__(self, *exts):
        self.exts = list(exts)
        self.listed = 0

    def list_installed(self):
        self.listed += 1
        return self.exts


SPOTIFY_WEB = _installed(
    "spotify-web",
    ["metadata_provider"],
    ["open.spotify.com", "spotify.com", "spotify:"],
)
APPLE_MUSIC = _installed(
    "apple-music", ["metadata_provider", "lyrics_provider"], ["music.apple.com"]
)

ALBUM_RESPONSE = {
    "success": True,
    "type": "album",
    "name": "Palette",
    "cover_url": "https://i.scdn.co/image/cover",
    "album": {"id": "5V8n6fqyAPxvFTibPhQVcp", "name": "Palette", "tracks": []},
    "tracks": [
        {
            "id": "4NPARrLIbtMl29ZJv8ESr2",
            "spotify_id": "4NPARrLIbtMl29ZJv8ESr2",
            "name": "dlwlrma",
            "artists": "IU",
            "album_name": "Palette",
            "album_artist": "IU",
            "duration_ms": 176098,
            "cover_url": "https://i.scdn.co/image/cover",
            "release_date": "2017-04-21",
            "track_number": 1,
            "total_tracks": 10,
            "disc_number": 1,
            "external_urls": "https://open.spotify.com/track/4NPARrLIbtMl29ZJv8ESr2",
            "isrc": "",
            "label": "EDAM Entertainment",
            "explicit": False,
        }
    ],
}


class _Provider:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def _call(self, method, *args):
        self.calls.append((method, args))
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def native(monkeypatch):
    """Stands in for the built-in client call; set `outcome` to a result or
    an exception."""
    state = {"outcome": ("Native", ["t"], "", {}), "calls": 0}

    async def fake_call(client, url, **kwargs):
        state["calls"] += 1
        if isinstance(state["outcome"], BaseException):
            raise state["outcome"]
        return state["outcome"]

    monkeypatch.setattr(downloader_module, "_call_metadata_get_url", fake_call)
    return state


def _run(url, *, native_factory=lambda: object(), manager=None, provider=None):
    return asyncio.run(
        get_url_with_fallback(
            url,
            native_factory,
            manager=manager or _Manager(SPOTIFY_WEB, APPLE_MUSIC),
            provider_factory=(lambda name: provider) if provider else None,
        )
    )


def test_a_working_built_in_client_is_all_that_is_asked(native) -> None:
    manager = _Manager(SPOTIFY_WEB)
    assert _run(SPOTIFY_ALBUM, manager=manager) == ("Native", ["t"], "", {})
    assert manager.listed == 0  # the extensions were not even looked at


def test_a_failing_spotify_client_hands_over_to_spotify_web(native) -> None:
    native["outcome"] = SpotiflacError(ErrorKind.AUTH_FAILED, "token refused")
    provider = _Provider(ALBUM_RESPONSE)

    name, tracks, cover, meta = _run(SPOTIFY_ALBUM, provider=provider)

    assert provider.calls == [("handleUrl", (SPOTIFY_ALBUM,))]
    assert provider.closed
    assert (name, cover, meta) == ("Palette", "https://i.scdn.co/image/cover", {})
    [track] = tracks
    # The id the built-in client gives, so downstream sees the same track.
    assert track.id == "4NPARrLIbtMl29ZJv8ESr2"
    assert (track.title, track.album, track.track_number) == ("dlwlrma", "Palette", 1)
    assert track.external_url == "https://open.spotify.com/track/4NPARrLIbtMl29ZJv8ESr2"
    assert track.publisher == "EDAM Entertainment"


def test_building_the_client_is_where_spotify_fails_first(native) -> None:
    def broken_factory():
        raise RuntimeError("could not get a client token")

    provider = _Provider(ALBUM_RESPONSE)
    _name, tracks, _cover, _meta = _run(
        SPOTIFY_ALBUM, native_factory=broken_factory, provider=provider
    )
    assert native["calls"] == 0
    assert tracks[0].title == "dlwlrma"


def test_an_empty_answer_is_also_a_reason_to_ask(native) -> None:
    native["outcome"] = ("Palette", [], "", {})
    _name, tracks, _cover, _meta = _run(
        SPOTIFY_ALBUM, provider=_Provider(ALBUM_RESPONSE)
    )
    assert len(tracks) == 1


def test_apple_music_tracks_keep_the_built_in_id_prefix(native) -> None:
    native["outcome"] = SpotiflacError(ErrorKind.NETWORK_ERROR, "timeout")
    response = {
        "success": True,
        "type": "track",
        "track": {
            "id": "1229073310",
            "name": "Palette (feat. G-DRAGON)",
            "artists": "IU",
        },
    }
    provider = _Provider(response)
    name, [track], _cover, _meta = _run(APPLE_ALBUM, provider=provider)
    assert track.id == "apple_1229073310"
    assert name == "Palette (feat. G-DRAGON)"


def test_a_malformed_link_is_not_sent_anywhere_else(native) -> None:
    native["outcome"] = SpotiflacError(ErrorKind.INVALID_URL, "not a Spotify link")
    manager = _Manager(SPOTIFY_WEB)
    with pytest.raises(SpotiflacError) as err:
        _run(SPOTIFY_ALBUM, manager=manager)
    assert err.value.kind == ErrorKind.INVALID_URL
    assert manager.listed == 0


def test_with_no_extension_the_built_in_error_is_what_the_caller_sees(native) -> None:
    native["outcome"] = SpotiflacError(ErrorKind.AUTH_FAILED, "token refused")
    with pytest.raises(SpotiflacError, match="token refused"):
        _run(SPOTIFY_ALBUM, manager=_Manager(APPLE_MUSIC))


def test_when_the_extension_fails_too_the_first_error_wins(native) -> None:
    native["outcome"] = SpotiflacError(ErrorKind.AUTH_FAILED, "token refused")
    provider = _Provider(
        {"success": False, "error": "utils.hmacSHA1 is not a function"}
    )
    with pytest.raises(SpotiflacError, match="token refused"):
        _run(SPOTIFY_ALBUM, provider=provider)


def test_services_without_a_stand_in_are_not_looked_up(native) -> None:
    native["outcome"] = SpotiflacError(ErrorKind.NETWORK_ERROR, "tidal down")
    manager = _Manager(SPOTIFY_WEB)
    with pytest.raises(SpotiflacError, match="tidal down"):
        _run("https://tidal.com/browse/album/1", manager=manager)
    assert manager.listed == 0


# ---------------------------------------------------------------------------
# Choosing the extension
# ---------------------------------------------------------------------------


def test_only_a_metadata_extension_whose_handler_claims_the_link_is_chosen() -> None:
    downloader_ext = _installed(
        "spotify-dl", ["download_provider"], ["open.spotify.com"]
    )
    disabled = _installed(
        "spotify-off", ["metadata_provider"], ["open.spotify.com"], enabled=False
    )
    manager = _Manager(downloader_ext, disabled, APPLE_MUSIC, SPOTIFY_WEB)
    assert fallback_extension_for(SPOTIFY_ALBUM, manager) == "spotify-web"
    assert fallback_extension_for(APPLE_ALBUM, manager) == "apple-music"
    assert fallback_extension_for("https://tidal.com/album/1", manager) is None


def test_external_links_given_as_a_map_still_become_a_link() -> None:
    track = track_from_extension(
        {
            "id": "1",
            "name": "x",
            "external_urls": {"spotify": "https://open.spotify.com/track/1"},
        },
        "spotify",
    )
    assert track is not None
    assert track.external_url == "https://open.spotify.com/track/1"


# ---------------------------------------------------------------------------
# Downloading never goes to a metadata-only extension
# ---------------------------------------------------------------------------


def test_a_metadata_extension_under_a_service_alias_is_not_used_to_download(
    monkeypatch,
) -> None:
    import SpotiFLAC.extensions.manager as manager_module
    import SpotiFLAC.extensions.provider as provider_module

    built = []

    class _FakeManager:
        def __init__(self, *args, **kwargs):
            pass

        def get_installed(self, ext_id):
            return APPLE_MUSIC if ext_id == "apple-music" else None

        def find_python_extension(self, base_name):
            return None

    class _FakeJS:
        def __init__(self, ext_id, **kwargs):
            built.append(ext_id)

    monkeypatch.setattr(manager_module, "ExtensionManager", _FakeManager)
    monkeypatch.setattr(provider_module, "JSExtensionProvider", _FakeJS)

    opts = downloader_module.DownloadOptions(output_dir=".")
    assert downloader_module._build_providers_for_name("apple", opts) == []
    assert built == []

    # A real download extension under an alias is still paired as before.
    class _DownloaderManager(_FakeManager):
        def get_installed(self, ext_id):
            if ext_id != "tidal-web":
                return None
            return _installed(ext_id, ["download_provider"], [])

    monkeypatch.setattr(manager_module, "ExtensionManager", _DownloaderManager)
    assert len(downloader_module._build_providers_for_name("tidal", opts)) == 1
    assert built == ["tidal-web"]

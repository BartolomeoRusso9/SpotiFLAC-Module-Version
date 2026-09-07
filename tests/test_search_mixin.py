"""`api_mixins.search` — the shaping that used to exist twice.

`app.py` held two line-for-line copies of the same reshaping, one in
`search_provider` and one in the thread `search_provider_async` starts. They
are one function now, and the point of these tests is the thing that made the
duplication dangerous in the first place: the output keys. The GUI's
JavaScript reads `name`/`artists`/`images`; older callers read
`title`/`artist`/`cover`. Both spellings have to survive.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.api_mixins.search import (
    SearchMixin,
    empty_results,
    search_metadata_async,
    shape_search_results,
)


class _Track:
    id = "t1"
    title = "So What"
    artists = "Miles Davis"
    album = "Kind of Blue"
    duration_ms = 545_000
    cover_url = "https://img.example/cover.jpg"
    external_url = "https://open.spotify.com/track/t1"
    preview_url = ""
    plays = "12345"
    is_explicit = False
    isrc = "USSM15900001"


def _results() -> dict:
    return {
        "tracks": [_Track()],
        "albums": [],
        "artists": [],
        "playlists": [],
    }


def test_every_section_is_present_even_when_empty() -> None:
    shaped = shape_search_results({})
    assert set(shaped) == {"tracks", "albums", "artists", "playlists"}
    assert all(section == [] for section in shaped.values())


def test_both_key_spellings_survive() -> None:
    """The GUI reads one set, older callers read the other."""
    track = shape_search_results(_results())["tracks"][0]

    assert track["name"] == track["title"] == "So What"
    assert track["artists"] == track["artist"] == "Miles Davis"
    assert track["images"] == track["cover"] == "https://img.example/cover.jpg"
    assert track["external_urls"] == track["external_url"]
    assert track["album_name"] == track["album"] == "Kind of Blue"
    assert track["type"] == "track"
    assert track["provider"] == "spotify"


def test_a_missing_field_costs_that_field_and_not_the_search() -> None:
    class _Sparse:
        title = "Untitled"

    shaped = shape_search_results({"tracks": [_Sparse()]})
    assert shaped["tracks"][0]["name"] == "Untitled"
    assert shaped["tracks"][0]["artists"] == ""
    assert shaped["tracks"][0]["isrc"] == ""


def test_the_limit_is_applied_per_section() -> None:
    shaped = shape_search_results({"tracks": [_Track()] * 10}, limit=3)
    assert len(shaped["tracks"]) == 3


def test_empty_results_hands_back_a_fresh_object() -> None:
    """A shared empty dict would let one caller's append reach every other."""
    first = empty_results()
    first["tracks"].append("x")
    assert empty_results()["tracks"] == []


def test_search_metadata_async_short_circuits_on_an_empty_query() -> None:
    """No query means no request — not a request for nothing."""
    assert asyncio.run(search_metadata_async("")) == empty_results()


def test_search_metadata_async_shapes_what_the_client_returns(monkeypatch) -> None:
    import SpotiFLAC.core.spotify_metadata as metadata_module

    class _Client:
        async def search_async(self, query, limit=50):
            assert query == "miles"
            return _results()

    monkeypatch.setattr(metadata_module, "SpotifyMetadataClient", _Client)

    shaped = asyncio.run(search_metadata_async("miles"))
    assert shaped["tracks"][0]["title"] == "So What"


# ---------------------------------------------------------------------------
# The blocking entry points the desktop GUI needs
# ---------------------------------------------------------------------------


class _Api(SearchMixin):
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.pushed: list[tuple[str, object]] = []

    def log(self, message, level="info") -> None:
        self.logs.append((message, level))

    def _push(self, event, payload) -> None:
        self.pushed.append((event, payload))


def test_search_provider_returns_the_shaped_results(monkeypatch) -> None:
    import SpotiFLAC.core.spotify_metadata as metadata_module

    class _Client:
        def search(self, query, limit=50):
            return _results()

    monkeypatch.setattr(metadata_module, "SpotifyMetadataClient", _Client)

    shaped = _Api().search_provider("miles")
    assert shaped["tracks"][0]["name"] == "So What"


def test_search_provider_reports_a_failure_instead_of_raising(monkeypatch) -> None:
    import SpotiFLAC.core.spotify_metadata as metadata_module

    class _Angry:
        def search(self, query, limit=50):
            msg = "spotify said no"
            raise RuntimeError(msg)

    monkeypatch.setattr(metadata_module, "SpotifyMetadataClient", _Angry)

    api = _Api()
    assert api.search_provider("miles") == empty_results()
    assert any("spotify said no" in message for message, _ in api.logs)


def test_an_empty_query_never_starts_a_thread() -> None:
    assert _Api().search_provider_async("") == {"status": "empty"}


def test_the_thread_pushes_results_to_the_window(monkeypatch) -> None:
    import SpotiFLAC.core.spotify_metadata as metadata_module

    class _Client:
        def search(self, query, limit=50):
            return _results()

    monkeypatch.setattr(metadata_module, "SpotifyMetadataClient", _Client)

    api = _Api()
    api._search_provider_thread("miles", 50)

    assert api.pushed[0][0] == "app_handle_provider_search_results"
    assert api.pushed[0][1]["tracks"][0]["title"] == "So What"


def test_the_thread_pushes_an_error_instead_of_dying(monkeypatch) -> None:
    import SpotiFLAC.core.spotify_metadata as metadata_module

    class _Angry:
        def search(self, query, limit=50):
            msg = "spotify said no"
            raise RuntimeError(msg)

    monkeypatch.setattr(metadata_module, "SpotifyMetadataClient", _Angry)

    api = _Api()
    api._search_provider_thread("miles", 50)

    assert api.pushed == [("app_handle_provider_search_error", "spotify said no")]


def test_a_window_that_has_gone_away_does_not_crash_the_search(monkeypatch) -> None:
    import SpotiFLAC.core.spotify_metadata as metadata_module

    class _Client:
        def search(self, query, limit=50):
            return _results()

    monkeypatch.setattr(metadata_module, "SpotifyMetadataClient", _Client)

    class _Closed(_Api):
        def _push(self, event, payload) -> None:
            msg = "window is gone"
            raise RuntimeError(msg)

    _Closed()._search_provider_thread("miles", 50)  # must not raise


def test_the_gui_api_still_offers_both_entry_points() -> None:
    """The mixin has to be mixed in, not merely written."""
    pytest.importorskip("webview")
    from SpotiFLAC.app import SpotiFLAC_API

    assert issubclass(SpotiFLAC_API, SearchMixin)
    assert callable(SpotiFLAC_API.search_provider)
    assert callable(SpotiFLAC_API.search_provider_async)

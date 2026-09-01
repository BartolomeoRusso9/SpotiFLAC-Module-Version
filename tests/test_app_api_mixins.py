"""Regression coverage for the SpotiFLAC_API mixin split (see
SpotiFLAC/api_mixins/__init__.py). Nothing here should change behavior —
only guards against a future edit accidentally dropping a method, or
breaking the MRO, when app.py / api_mixins/* are touched again.
"""

from __future__ import annotations

from SpotiFLAC.api_mixins.covers_lyrics import CoversLyricsMixin
from SpotiFLAC.api_mixins.local_tagging import LocalTaggingMixin
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.webapp import ALLOWED_METHODS


def test_api_exposes_every_web_allowed_method() -> None:
    """Every method webapp.py is willing to dispatch over HTTP must actually
    exist on the composed object — whether it's defined directly on
    SpotiFLAC_API or inherited from one of its mixins.
    """
    api = SpotiFLAC_API()
    missing = [m for m in ALLOWED_METHODS if not callable(getattr(api, m, None))]
    assert missing == []


def test_moved_methods_resolve_to_their_mixin() -> None:
    """Local-tagging and cover/lyrics methods should come from the mixin
    they were extracted into, not be silently re-defined on SpotiFLAC_API
    (which would shadow the mixin and defeat the point of the split).
    """
    for name in ("scan_local", "apply_local_tags"):
        assert name in LocalTaggingMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith(
            "LocalTaggingMixin."
        )

    for name in (
        "download_track_lyrics",
        "download_track_cover",
        "download_cover",
        "download_album_cover",
        "download_all_covers",
        "download_all_lyrics",
    ):
        assert name in CoversLyricsMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith(
            "CoversLyricsMixin."
        )


def test_moved_methods_behave_the_same_on_empty_input() -> None:
    """Cheap, synchronous edge cases that don't touch the network or spawn
    background threads — enough to prove the moved code still runs, not a
    full functional test of the local-tagging feature.
    """
    api = SpotiFLAC_API()
    assert api.scan_local("") == {"status": "error", "error": "No path given"}
    assert api.apply_local_tags([]) == {
        "status": "error",
        "error": "Nothing to apply",
    }


# ── Dashboard and CSV import (api_mixins/stats.py, api_mixins/csv_import.py) ─


def test_the_dashboard_is_served_from_the_bridge() -> None:
    from SpotiFLAC.core import download_log

    api = SpotiFLAC_API()
    download_log.record(title="Song", artist="Queen", provider="ext:tidal-web")

    document = api.get_stats()

    assert document["totals"]["tracks"] == 1
    assert document["top_artists"][0]["name"] == "Queen"


def test_a_dashboard_asked_for_by_an_account_covers_that_account() -> None:
    from SpotiFLAC.core import download_log

    download_log.record(title="Mine", artist="Queen", owner="ada")
    download_log.record(title="Theirs", artist="Prince", owner="grace")

    api = SpotiFLAC_API()
    api.owner = "ada"

    document = api.get_stats()
    assert document["owner"] == "ada"
    assert document["totals"]["tracks"] == 1
    assert document["top_artists"][0]["name"] == "Queen"

    # No owner set (desktop, single-user --web) means the whole instance.
    assert SpotiFLAC_API().get_stats()["totals"]["tracks"] == 2


def test_a_csv_preview_matches_without_downloading_anything() -> None:
    api = SpotiFLAC_API()
    content = (
        "Track URI,Track Name,Artist Name(s)\n"
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC,Never Gonna Give You Up,Rick Astley\n"
    )

    preview = api.preview_csv(content, name="export.csv")

    assert preview["ok"] is True
    assert preview["rows"] == 1
    assert preview["urls"] == ["https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"]
    assert api.current_tracks == []


def test_an_unreadable_csv_comes_back_as_an_error_not_an_exception() -> None:
    api = SpotiFLAC_API()

    assert api.preview_csv("")["ok"] is False
    assert api.preview_csv("\n\n")["ok"] is False
    assert api.fetch_csv("")["status"] == "error"
    # Refused before it is parsed, rather than read into the UI process.
    assert api.preview_csv("x" * 3_000_000)["ok"] is False


def test_a_nonsense_match_score_is_refused_before_anything_is_matched() -> None:
    """A negative or a NaN floor accepts every candidate, so a messy export
    downloads a wrong match under the right filename. The CLI and the REST
    schema check this; the GUI bridge took the number on trust.
    """
    api = SpotiFLAC_API()
    # A row that carries a link resolves without a catalogue search, so the
    # valid-threshold case below stays offline.
    content = (
        "Track URI,Track Name,Artist Name(s)\n"
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC,Everlong,Foo Fighters\n"
    )

    for bad in (-0.5, 1.5, float("nan"), "close enough"):
        assert api.preview_csv(content, min_score=bad)["ok"] is False, bad
        assert api.fetch_csv(content, min_score=bad)["status"] == "error", bad

    # A valid threshold still gets through, and so does "unset".
    assert api.preview_csv(content, min_score=0.9)["ok"] is True
    assert api.preview_csv(content)["ok"] is True


def test_a_csv_import_fills_the_track_table_like_a_link_does(monkeypatch) -> None:
    """The point of the CSV path: it ends where a pasted link ends, so the
    existing track table, selection and download button need no new case.
    """
    from SpotiFLAC.core.models import TrackMetadata

    api = SpotiFLAC_API()
    pushed: list[tuple] = []
    api._push = lambda name, *args: pushed.append((name, args))

    async def _tracks(urls, on_progress=None):
        assert urls == ["https://open.spotify.com/track/aaa"]
        return [
            TrackMetadata(
                id="aaa",
                title="Everlong",
                artists="Foo Fighters",
                album="The Colour and the Shape",
                album_artist="Foo Fighters",
            )
        ], 0

    monkeypatch.setattr(api, "_csv_tracks_async", _tracks)
    api._fetch_csv_thread(
        "https://open.spotify.com/track/aaa\n", "wishlist.csv", None, None
    )

    assert [track.title for track in api.current_tracks] == ["Everlong"]
    # No collection URL: a CSV is a list of songs, not a thing with a link,
    # and `_download_task` uses this to decide whether it can download "the
    # whole thing" in one go.
    assert api.current_url == ""

    events = {name: args for name, args in pushed}
    assert events["showTracklist"][0][0]["title"] == "Everlong"
    assert events["app_csv_loaded"][0]["tracks"] == 1


def test_a_csv_with_no_usable_row_reports_instead_of_loading() -> None:
    """A recognised header with nothing under it: the track table is left
    alone and the interface is told why, rather than being handed an empty
    list to render as a successful import.
    """
    api = SpotiFLAC_API()
    pushed: list[tuple] = []
    api._push = lambda name, *args: pushed.append((name, args))

    api._fetch_csv_thread("Track Name,Artist Name(s)\n", "wishlist.csv", None, None)

    assert api.current_tracks == []
    assert "app_csv_error" in {name for name, _ in pushed}

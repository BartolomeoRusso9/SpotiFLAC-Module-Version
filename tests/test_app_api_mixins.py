"""Regression coverage for the MornyeAPI mixin split (see
mornye/api_mixins/__init__.py). Nothing here should change behavior —
only guards against a future edit accidentally dropping a method, or
breaking the MRO, when app.py / api_mixins/* are touched again.
"""

from __future__ import annotations

from typing import Any, cast
from mornye.api_mixins.covers_lyrics import CoversLyricsMixin
from mornye.api_mixins.local_tagging import LocalTaggingMixin
from mornye.app import MornyeAPI
from mornye.webapp import ALLOWED_METHODS


def test_api_exposes_every_web_allowed_method() -> None:
    """Every method webapp.py is willing to dispatch over HTTP must actually
    exist on the composed object — whether it's defined directly on
    MornyeAPI or inherited from one of its mixins.
    """
    api = MornyeAPI()
    missing = [m for m in ALLOWED_METHODS if not callable(getattr(api, m, None))]
    assert missing == []


def test_moved_methods_resolve_to_their_mixin() -> None:
    """Local-tagging and cover/lyrics methods should come from the mixin
    they were extracted into, not be silently re-defined on MornyeAPI
    (which would shadow the mixin and defeat the point of the split).
    """
    for name in ("scan_local", "apply_local_tags"):
        assert name in LocalTaggingMixin.__dict__
        assert getattr(MornyeAPI, name).__qualname__.startswith("LocalTaggingMixin.")

    for name in (
        "download_track_lyrics",
        "download_track_cover",
        "download_cover",
        "download_album_cover",
        "download_all_covers",
        "download_all_lyrics",
    ):
        assert name in CoversLyricsMixin.__dict__
        assert getattr(MornyeAPI, name).__qualname__.startswith("CoversLyricsMixin.")


def test_moved_methods_behave_the_same_on_empty_input() -> None:
    """Cheap, synchronous edge cases that don't touch the network or spawn
    background threads — enough to prove the moved code still runs, not a
    full functional test of the local-tagging feature.
    """
    api = MornyeAPI()
    assert api.scan_local("") == {"status": "error", "error": "No path given"}
    assert api.apply_local_tags([]) == {
        "status": "error",
        "error": "Nothing to apply",
    }


# ── Dashboard and CSV import (api_mixins/stats.py, api_mixins/csv_import.py) ─


def test_the_dashboard_is_served_from_the_bridge() -> None:
    from mornye.core import download_log

    api = MornyeAPI()
    download_log.record(title="Song", artist="Queen", provider="ext:tidal-web")

    document = api.get_stats()

    assert document["totals"]["tracks"] == 1
    assert document["top_artists"][0]["name"] == "Queen"


def test_a_dashboard_asked_for_by_an_account_covers_that_account() -> None:
    from mornye.core import download_log

    download_log.record(title="Mine", artist="Queen", owner="ada")
    download_log.record(title="Theirs", artist="Prince", owner="grace")

    api = MornyeAPI()
    api.owner = "ada"

    document = api.get_stats()
    assert document["owner"] == "ada"
    assert document["totals"]["tracks"] == 1
    assert document["top_artists"][0]["name"] == "Queen"

    # No owner set (desktop, single-user --web) means the whole instance.
    assert MornyeAPI().get_stats()["totals"]["tracks"] == 2


def test_smb_library_scan_uses_saved_credentials_and_pushes_report(monkeypatch) -> None:
    from mornye.api_mixins import stats as stats_mixin
    from mornye.core import network_destinations, smb_library_scan

    profile = network_destinations.validate_profile(
        {
            "id": "destination1234",
            "name": "Home NAS",
            "host": "nas.example.net",
            "share": "Music",
            "username": "music-user",
        }
    )

    class DestinationStore:
        def get(self, profile_id):
            return profile if profile_id == profile.id else None

        def credentials(self, selected, *, owner):
            assert selected is profile
            assert owner == "ada"
            return "session-secret"

    class ImmediateThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            assert daemon is True

        def start(self):
            self.target(*self.args)

    pushed = []
    api = MornyeAPI()
    api.owner = "ada"
    api._push = lambda name, *args: pushed.append((name, args))

    def fake_scan(selected, username, password, *, progress):
        assert selected is profile
        assert username == "music-user"
        assert password == "session-secret"
        progress(1, 1)
        return {"source": "smb", "totals": {"tracks": 1}}

    monkeypatch.setattr(network_destinations, "DESTINATIONS", DestinationStore())
    monkeypatch.setattr(stats_mixin.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(smb_library_scan, "scan_smb_library", fake_scan)

    assert api.scan_smb_library(profile.id) == {"status": "started"}
    assert pushed == [
        ("app_smb_library_progress", ({"done": 1, "total": 1},)),
        ("app_smb_library_results", ({"source": "smb", "totals": {"tracks": 1}},)),
    ]


def test_a_csv_preview_matches_without_downloading_anything() -> None:
    api = MornyeAPI()
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
    api = MornyeAPI()

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
    api = MornyeAPI()
    # A row that carries a link resolves without a catalogue search, so the
    # valid-threshold case below stays offline.
    content = (
        "Track URI,Track Name,Artist Name(s)\n"
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC,Everlong,Foo Fighters\n"
    )

    for bad in (-0.5, 1.5, float("nan"), "close enough"):
        assert api.preview_csv(content, min_score=cast(Any, bad))["ok"] is False, bad
        assert (
            api.fetch_csv(content, min_score=cast(Any, bad))["status"] == "error"
        ), bad

    # A valid threshold still gets through, and so does "unset".
    assert api.preview_csv(content, min_score=0.9)["ok"] is True
    assert api.preview_csv(content)["ok"] is True


def test_a_csv_import_fills_the_track_table_like_a_link_does(monkeypatch) -> None:
    """The point of the CSV path: it ends where a pasted link ends, so the
    existing track table, selection and download button need no new case.
    """
    from mornye.core.models import TrackMetadata

    api = MornyeAPI()
    pushed: list[tuple] = []
    cast(Any, api)._push = lambda name, *args: pushed.append((name, args))

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


def test_a_repeated_link_is_counted_rather_than_fetched_twice(monkeypatch) -> None:
    """The same track twice in a file is one fetch (CsvResolution.urls), so
    the track table is shorter than the match count by the repeats. Saying
    so is what keeps the closing summary's numbers adding up: without it
    those rows look like rows that went missing.
    """
    from mornye.core.models import TrackMetadata

    api = MornyeAPI()
    pushed: list[tuple] = []
    cast(Any, api)._push = lambda name, *args: pushed.append((name, args))

    async def _tracks(urls, on_progress=None):
        assert urls == ["https://open.spotify.com/track/aaa"], "fetched once"
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
        "https://open.spotify.com/track/aaa\n" * 3, "wishlist.csv", None, None
    )

    loaded = {name: args for name, args in pushed}["app_csv_loaded"][0]
    assert loaded["rows"] == 3
    assert loaded["matched"] == 3
    assert loaded["tracks"] == 1
    assert loaded["duplicates"] == 2


def test_a_csv_with_no_usable_row_reports_instead_of_loading() -> None:
    """A recognised header with nothing under it: the track table is left
    alone and the interface is told why, rather than being handed an empty
    list to render as a successful import.
    """
    api = MornyeAPI()
    pushed: list[tuple] = []
    cast(Any, api)._push = lambda name, *args: pushed.append((name, args))

    api._fetch_csv_thread("Track Name,Artist Name(s)\n", "wishlist.csv", None, None)

    assert api.current_tracks == []
    assert "app_csv_error" in {name for name, _ in pushed}

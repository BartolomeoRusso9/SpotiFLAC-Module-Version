"""MusicBrainz tags for tracks whose ISRC nobody linked.

The lookup was ISRC-only. MusicBrainz knows plenty of recordings whose ISRC
was never entered — most of the Italian catalogue, in one measured library
47 of 165 files — so the search came back empty and the track got no
MusicBrainz tags at all, silently, while a search for the same title and
artist found it at score 100.

The fallback below is what closes that gap; these tests are about the
guards that keep it from writing another recording's MBIDs into the file.
"""

from __future__ import annotations

import logging

import pytest

from SpotiFLAC.core import musicbrainz as mb


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    # Both caches, or the test machine's own successful lookups (this module
    # persists them for 30 days) answer before the stubbed query ever runs.
    mb._mb_cache.clear()
    mb._mb_cache_order.clear()
    monkeypatch.setattr(mb, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(mb, "put_cached_response", lambda *a, **k: None)
    mb.set_mb_status(True)
    yield
    mb._mb_cache.clear()
    mb._mb_cache_order.clear()


def _recording(
    *,
    score=100,
    title="Butterfly Knife",
    artists=("Noyz Narcos", "Chicoria"),
    length=231000,
    rec_id="rec-1",
):
    return {
        "id": rec_id,
        "score": score,
        "title": title,
        "length": length,
        "artist-credit": [{"artist": {"id": f"a{i}", "name": n}} for i, n in enumerate(artists)],
    }


# ── the comparisons ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        ("Cyborg (feat. Geolier)", "Cyborg"),
        ("L'ultima volta - feat. Massimo Pericolo", "L'ultima volta"),
        ("MI Fist (2004 Remaster)", "MI Fist"),
        ("Phra (Outro)", "Phra"),
        ("Ciao fraté - Originale", "Ciao fraté"),
    ],
)
def test_titles_that_name_the_same_recording(ours, theirs) -> None:
    assert mb._title_matches(ours, theirs)


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        ("Love", "Lovers"),  # a prefix that is not a word boundary
        ("Wow.", "Wowie Zowie"),
        ("Stanza 106", "Stanza 107"),
    ],
)
def test_titles_that_do_not(ours, theirs) -> None:
    assert not mb._title_matches(ours, theirs)


def test_the_same_artist_written_two_ways_matches() -> None:
    # The rename: our metadata says "Guè", MusicBrainz still says the old name.
    assert mb._artist_matches("Guè", "Gué Pequeno")
    # And a joined credit against either of the names in it.
    assert mb._artist_matches("Noyz Narcos, Chicoria", "Noyz Narcos")


def test_a_different_artist_does_not_match() -> None:
    # Both released a "Stanza 106"; only one of them is ours.
    assert not mb._artist_matches("Guè", "Gemello")


def test_a_joined_credit_is_searched_for_by_its_first_name() -> None:
    """MusicBrainz matches the artist query against one credited name at a
    time, so querying the whole joined string finds nothing at all."""
    assert mb._primary_artist("Noyz Narcos, Chicoria") == "Noyz Narcos"
    assert mb._primary_artist("Drake & Future") == "Drake"
    assert mb._primary_artist("Ernia feat. Rkomi") == "Ernia"
    query = mb._fallback_query("Butterfly Knife", "Noyz Narcos, Chicoria")
    assert query == 'recording:"butterfly knife" AND artist:"noyz narcos"'


# ── the guards ────────────────────────────────────────────────────────────


def _pick(recordings, *, title="Butterfly Knife", artist="Noyz Narcos, Chicoria", duration_ms=231266):
    return mb._pick_fallback_recording(
        recordings, title=title, artist=artist, duration_ms=duration_ms
    )


def test_a_matching_recording_is_accepted() -> None:
    assert _pick([_recording()])["id"] == "rec-1"


def test_a_recording_of_a_different_length_is_refused() -> None:
    """The live version, the re-recording and the cover all pass a title and
    artist check; the running time is what separates them."""
    assert _pick([_recording(length=272000)]) is None


def test_a_low_search_score_is_refused() -> None:
    assert _pick([_recording(score=80)]) is None


def test_the_first_candidate_that_clears_every_guard_wins() -> None:
    picked = _pick(
        [
            _recording(score=100, length=272000, rec_id="live"),
            _recording(score=97, length=231500, rec_id="studio"),
        ]
    )
    assert picked["id"] == "studio"


# MusicBrainz frequently has no length at all for a recording, which is why
# there is a second, stricter reading rather than a flat refusal.


def test_a_lengthless_recording_is_accepted_on_the_strict_reading() -> None:
    assert _pick([_recording(length=None)])["id"] == "rec-1"


def test_a_lengthless_recording_needs_the_same_set_of_artists() -> None:
    # A cover: same title, same score, no length — and someone else playing.
    assert _pick([_recording(length=None, artists=("Salmo",))]) is None
    # Only part of the credit is not the same credit.
    assert _pick([_recording(length=None, artists=("Noyz Narcos",))]) is None


def test_a_lengthless_recording_needs_a_near_perfect_score() -> None:
    assert _pick([_recording(length=None, score=96)]) is None


def test_a_lengthless_recording_needs_an_equal_title() -> None:
    # "Cyborg (feat. Geolier)" may meet "Cyborg" when a duration backs it up;
    # with nothing to check the length against, it may not.
    assert (
        mb._pick_fallback_recording(
            [_recording(length=None, title="Butterfly Knife (Live)")],
            title="Butterfly Knife",
            artist="Noyz Narcos, Chicoria",
            duration_ms=231266,
        )
        is None
    )


def test_our_own_missing_duration_still_allows_the_strict_reading() -> None:
    assert _pick([_recording(length=231000)], duration_ms=0)["id"] == "rec-1"


# ── the lookup itself ─────────────────────────────────────────────────────


def test_the_fallback_only_runs_when_the_isrc_found_nothing(monkeypatch) -> None:
    queries: list[str] = []

    def _fake_query(query: str) -> dict:
        queries.append(query)
        if query.startswith("isrc:"):
            return {"recordings": [_recording(rec_id="by-isrc")]}
        return {"recordings": [_recording(rec_id="by-title")]}

    monkeypatch.setattr(mb, "_query_recordings", _fake_query)
    monkeypatch.setattr(mb, "_query_recording_details", lambda _id: {})

    res = mb.fetch_mb_metadata(
        "ITDF61777027",
        title="Butterfly Knife",
        artist="Noyz Narcos, Chicoria",
        duration_ms=231266,
    )
    assert res["mbid_track"] == "by-isrc"
    assert queries == ["isrc:ITDF61777027"], "a hit must not cost a second query"


def test_an_unlinked_isrc_falls_back_and_says_so(monkeypatch, caplog) -> None:
    def _fake_query(query: str) -> dict:
        if query.startswith("isrc:"):
            return {"recordings": []}
        return {"recordings": [_recording(rec_id="by-title")]}

    monkeypatch.setattr(mb, "_query_recordings", _fake_query)
    monkeypatch.setattr(mb, "_query_recording_details", lambda _id: {})

    with caplog.at_level(logging.INFO, logger="SpotiFLAC.core.musicbrainz"):
        res = mb.fetch_mb_metadata(
            "ITDF61777027",
            title="Butterfly Knife",
            artist="Noyz Narcos, Chicoria",
            duration_ms=231266,
        )

    assert res["mbid_track"] == "by-title"
    assert "is not linked on MusicBrainz" in caplog.text
    assert "ITDF61777027" in caplog.text


def test_no_match_anywhere_is_logged_rather_than_swallowed(monkeypatch, caplog) -> None:
    """Nothing was logged at all when MusicBrainz simply had no match, which
    made "no tags" indistinguishable from a broken lookup."""
    monkeypatch.setattr(mb, "_query_recordings", lambda _q: {"recordings": []})
    monkeypatch.setattr(mb, "_query_recording_details", lambda _id: {})

    with caplog.at_level(logging.INFO, logger="SpotiFLAC.core.musicbrainz"):
        res = mb.fetch_mb_metadata(
            "ITC890400137", title="Ciao fraté", artist="Cor Veleno", duration_ms=193200
        )

    assert not any(res.values())
    assert "no match for ISRC ITC890400137" in caplog.text


def test_a_failed_lookup_is_logged_as_a_warning(monkeypatch, caplog) -> None:
    def _boom(_q):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(mb, "_query_recordings", _boom)

    with caplog.at_level(logging.INFO, logger="SpotiFLAC.core.musicbrainz"):
        assert mb.fetch_mb_metadata("ITUM72000287") == {}

    assert "lookup failed for ISRC ITUM72000287" in caplog.text
    assert "HTTP 503" in caplog.text


def test_the_pause_after_a_failure_says_how_long_it_has_left(caplog) -> None:
    """One failed request silences every lookup for 30s. That is deliberate —
    MusicBrainz rate-limits hard — but it has to be visible, or a whole batch
    of untagged tracks looks like a bug."""
    mb.set_mb_status(False)
    try:
        assert mb.should_skip_mb()
        assert 0 < mb.mb_skip_remaining() <= mb._MB_STATUS_SKIP_WINDOW
        with caplog.at_level(logging.INFO, logger="SpotiFLAC.core.musicbrainz"):
            assert mb.fetch_mb_metadata("ITDF61777027") == {}
        assert "lookups paused" in caplog.text
    finally:
        mb.set_mb_status(True)


def test_the_async_twin_falls_back_too(monkeypatch) -> None:
    """The module keeps a sync and an async copy of this lookup, and the
    downloads go through the async one — a guard added to only one of them
    would be a guard the app never runs."""
    import asyncio

    async def _fake_query(query: str) -> dict:
        if query.startswith("isrc:"):
            return {"recordings": []}
        return {"recordings": [_recording(rec_id="by-title")]}

    async def _no_details(_id: str) -> dict:
        return {}

    monkeypatch.setattr(mb, "_query_recordings_async", _fake_query)
    monkeypatch.setattr(mb, "_query_recording_details_async", _no_details)

    res = asyncio.run(
        mb.fetch_mb_metadata_async(
            "ITDF61777027",
            title="Butterfly Knife",
            artist="Noyz Narcos, Chicoria",
            duration_ms=231266,
        )
    )
    assert res["mbid_track"] == "by-title"


def test_callers_that_pass_no_title_behave_exactly_as_before(monkeypatch) -> None:
    queries: list[str] = []

    def _fake_query(query: str) -> dict:
        queries.append(query)
        return {"recordings": []}

    monkeypatch.setattr(mb, "_query_recordings", _fake_query)
    monkeypatch.setattr(mb, "_query_recording_details", lambda _id: {})

    assert not any(mb.fetch_mb_metadata("ITDF61777027").values())
    assert queries == ["isrc:ITDF61777027"], "no title, no fallback query"

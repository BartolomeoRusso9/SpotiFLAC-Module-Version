"""Identifying a local file by sound, and — more importantly — when not to.

The fingerprint path spends a rate limit shared by every SpotiFLAC user (see
core/acoustid_lookup.py), so "it runs only on the files the cheap path could
not resolve" is a correctness property, not an optimisation. Half of this
file is about the requests that must *not* be made.

Nothing here touches the network or fpcalc: the lookup response and the
fingerprint are both stubbed, so the tests exercise the decision logic
rather than AcoustID's availability.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import acoustid_lookup
from SpotiFLAC.core.audio_fingerprint import AudioFingerprint
from SpotiFLAC.core.local_matcher import MatchCandidate
from SpotiFLAC.core.local_processor import LocalScanEntry, _needs_identifying
from SpotiFLAC.core.local_scanner import LocalFileInfo
from SpotiFLAC.core.models import TrackMetadata

ISRC = "GBAYE0601498"


def _ok_response(isrc: str = ISRC, score: float = 1.0) -> dict:
    return {
        "status": "ok",
        "results": [{"score": score, "recordings": [{"id": "rec-1", "isrcs": [isrc]}]}],
    }


def _entry(*, error="", isrc="", safe=False, has_candidate=True) -> LocalScanEntry:
    info = LocalFileInfo(file_path="/m/x.flac", error=error, old_isrc=isrc)
    candidates = []
    if has_candidate:
        candidates = [
            MatchCandidate(
                metadata=TrackMetadata(
                    id="0" * 22, title="t", artists="a", album="", album_artist="a"
                ),
                confidence=100.0 if safe else 40.0,
                title_ratio=1.0 if safe else 0.4,
            )
        ]
    return LocalScanEntry(info=info, candidates=candidates)


# --- which files are worth spending a lookup on -----------------------------


def test_a_safely_matched_file_is_left_alone() -> None:
    """It cost nothing and a fingerprint could not improve on it."""
    assert not _needs_identifying(_entry(safe=True))


def test_a_file_that_already_names_its_recording_is_left_alone() -> None:
    """An ISRC in the tags is the same identity a lookup would return, for
    free — asking AcoustID would spend the shared budget to learn nothing.
    """
    assert not _needs_identifying(_entry(isrc=ISRC, safe=False))


def test_an_unreadable_file_is_left_alone() -> None:
    assert not _needs_identifying(_entry(error="Could not read tags"))


def test_an_unsafe_match_is_worth_identifying() -> None:
    assert _needs_identifying(_entry(safe=False))


def test_a_file_with_no_match_at_all_is_worth_identifying() -> None:
    assert _needs_identifying(_entry(has_candidate=False))


# --- reading the lookup response -------------------------------------------


def test_isrc_is_read_from_the_best_scoring_result() -> None:
    payload = {
        "status": "ok",
        "results": [
            {"score": 0.4, "recordings": [{"isrcs": ["USRC17607839"]}]},
            {"score": 0.99, "recordings": [{"isrcs": [ISRC]}]},
        ],
    }
    assert acoustid_lookup._best_isrc(payload) == (ISRC, 0.99)


def test_a_result_without_isrcs_is_not_an_answer() -> None:
    """AcoustID can know the recording and have no ISRC for it."""
    payload = {"status": "ok", "results": [{"score": 1.0, "recordings": [{"id": "r"}]}]}
    assert acoustid_lookup._best_isrc(payload) == ("", 0.0)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "error", "error": {"message": "invalid API key"}},
        {"status": "ok"},
        {"status": "ok", "results": []},
        {"status": "ok", "results": [{"score": "not a number"}]},
        {"status": "ok", "results": [{"score": 1.0, "recordings": None}]},
    ],
)
def test_malformed_responses_yield_no_answer(payload) -> None:
    assert acoustid_lookup._best_isrc(payload) == ("", 0.0)


# --- the lookup itself ------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_no_key_means_no_request(monkeypatch) -> None:
    """Without a key there is nothing to send, and fpcalc must not even run."""
    monkeypatch.setattr(acoustid_lookup, "_configured_key", lambda k="": ("", "u"))

    def _boom(*a, **k):
        raise AssertionError("fingerprinting must not run without a key")

    monkeypatch.setattr(acoustid_lookup, "compute_fingerprint_async", _boom)
    assert _run(acoustid_lookup.identify_isrc_async("/m/x.flac")) == ""


def test_a_low_score_is_discarded(monkeypatch) -> None:
    """A loose fingerprint match is worse than no answer: it would be handed
    to the matcher as an identity, which bypasses every text safeguard.
    """
    _stub_fingerprint(monkeypatch)
    _stub_post(monkeypatch, _ok_response(score=0.5))
    assert _run(acoustid_lookup.identify_isrc_async("/m/x.flac")) == ""


def test_a_confident_match_returns_the_isrc(monkeypatch) -> None:
    _stub_fingerprint(monkeypatch)
    _stub_post(monkeypatch, _ok_response(score=0.98))
    assert _run(acoustid_lookup.identify_isrc_async("/m/x.flac")) == ISRC


def test_a_network_failure_is_not_an_exception(monkeypatch) -> None:
    """One unreachable lookup must not abort a folder scan."""
    _stub_fingerprint(monkeypatch)

    class _Client:
        async def post(self, *a, **k):
            raise OSError("connection reset")

    _install_client(monkeypatch, _Client())
    assert _run(acoustid_lookup.identify_isrc_async("/m/x.flac")) == ""


def test_the_request_carries_what_acoustid_requires(monkeypatch) -> None:
    """client, fingerprint and duration are mandatory, and the ISRCs only
    come back when they are asked for by name.
    """
    _stub_fingerprint(monkeypatch, duration_s=241.6)
    sent: dict = {}

    class _Client:
        async def post(self, url, **kwargs):
            sent["url"] = url
            sent["data"] = kwargs.get("data")
            return _Response(_ok_response())

    _install_client(monkeypatch, _Client())
    _run(acoustid_lookup.identify_isrc_async("/m/x.flac", settings_key="MYKEY"))

    assert sent["data"]["client"] == "MYKEY", "a user's own key must win"
    assert sent["data"]["fingerprint"] == "FPCOMPRESSED"
    assert sent["data"]["duration"] == "242", "seconds, rounded, as a string"
    assert sent["data"]["meta"] == "isrcs", (
        "combining meta values ('recordings+isrcs') makes AcoustID return "
        "results with no recordings attached, silently — verified live"
    )


# --- helpers ----------------------------------------------------------------


class _Response:
    status_code = 200

    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _stub_fingerprint(monkeypatch, *, duration_s: float = 240.0) -> None:
    async def _fake(path):
        return AudioFingerprint(
            path=path, duration_s=duration_s, raw=(1, 2, 3), compressed="FPCOMPRESSED"
        )

    monkeypatch.setattr(acoustid_lookup, "compute_fingerprint_async", _fake)
    monkeypatch.setattr(
        acoustid_lookup, "_configured_key", lambda k="": (k or "SHARED", "https://u")
    )


def _stub_post(monkeypatch, payload) -> None:
    """Stubs the shared httpx client the lookup posts through."""

    class _Client:
        async def post(self, *a, **k):
            return _Response(payload)

    _install_client(monkeypatch, _Client())


def _install_client(monkeypatch, client) -> None:
    class _Manager:
        @staticmethod
        async def get_async_client_safe():
            return client

    monkeypatch.setattr(acoustid_lookup, "NetworkManager", _Manager)


# --- turning an identified ISRC into a Spotify track ------------------------


def _search_stub(tracks):
    class _Client:
        def search(self, query, limit=5):
            _Client.last_query = query
            return {"tracks": tracks}

        async def get_track_async(self, track_id):
            return tracks[0]

    return _Client()


def test_isrc_is_resolved_by_identity_not_by_the_files_own_text() -> None:
    """The file's text is what failed in the first place. Searching by it
    again answered a file guessed as "01" with a track called "010"; the
    query has to be the ISRC.
    """
    from SpotiFLAC.core.local_processor import _track_for_isrc

    track = TrackMetadata(
        id="1" * 22,
        title="Window Shopper",
        artists="50 Cent",
        album="a",
        album_artist="50 Cent",
        duration_ms=192_240,
    )
    client = _search_stub([track])
    candidate = asyncio.run(_track_for_isrc("USUM70504267", client=client))

    assert type(client).last_query == "isrc:USUM70504267"
    assert candidate is not None
    assert candidate.how == "isrc"
    assert candidate.is_safe


def test_a_duration_that_disagrees_rejects_the_isrc() -> None:
    """An ISRC shared by, or misattributed to, a different cut. Both numbers
    are already in hand, so the check is free.
    """
    from SpotiFLAC.core.local_processor import _track_for_isrc

    track = TrackMetadata(
        id="1" * 22,
        title="Window Shopper (Extended)",
        artists="50 Cent",
        album="a",
        album_artist="50 Cent",
        duration_ms=600_000,
    )
    candidate = asyncio.run(
        _track_for_isrc(
            "USUM70504267", client=_search_stub([track]), expected_duration_ms=190_000
        )
    )
    assert candidate is None


def test_an_isrc_spotify_does_not_know_yields_nothing() -> None:
    from SpotiFLAC.core.local_processor import _track_for_isrc

    assert asyncio.run(_track_for_isrc("ZZZZZ0000000", client=_search_stub([]))) is None

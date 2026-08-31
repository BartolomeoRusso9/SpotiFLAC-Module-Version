"""The guard that catches a provider resolving the wrong *recording*.

Every case below is one that actually happened in a single playlist run —
see core/recording_guard.py for the full account.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import recording_guard
from SpotiFLAC.core.recording_guard import (
    judge_resolved_recording,
    wrong_recording_reason_async,
)

LIKE_HIM = {
    "title": "Like Him (feat. Lola Young)",
    "artist": "Tyler, The Creator, Lola Young",
    "duration_ms": 278014,
    "explicit": True,
}


def _judge(found, expected=None):
    want = {**LIKE_HIM, **(expected or {})}
    return judge_resolved_recording(
        expected_title=want["title"],
        expected_artist=want["artist"],
        expected_duration_ms=want["duration_ms"],
        expected_explicit=want["explicit"],
        found=found,
    )


# --- what has to be rejected ------------------------------------------------


def test_a_karaoke_take_is_rejected() -> None:
    """Same song, same length, no voice. The title decoration is the only
    difference and strip_noise() erases it, so the variant check has to run
    before the titles are compared at all.
    """
    reason = _judge(
        {
            "title": (
                "Like Him (Feat. Lola Young) (By Tyler, The Creator) "
                "(Melody Karaoke Version)"
            ),
            "artist": "ZZang KARAOKE",
            "duration_ms": 279000,
            "explicit": False,
        },
    )
    assert "different version" in reason


def test_a_clean_edit_of_an_explicit_track_is_rejected() -> None:
    """The hard one: "Hours In Silence" USUG12208620 against the requested
    USUG12208604. Same title, same artist, same 399 seconds — the words are
    muted and the explicit flag is the only place that is written down.
    """
    reason = _judge(
        {
            "title": "Hours In Silence",
            "artist": "Drake",
            "duration_ms": 399000,
            "explicit": False,
        },
        expected={
            "title": "Hours In Silence",
            "artist": "Drake, 21 Savage",
            "duration_ms": 399153,
        },
    )
    assert "clean edit" in reason


def test_a_cover_by_another_artist_is_rejected() -> None:
    assert "different artist" in _judge(
        {
            "title": "Like Him",
            "artist": "Some Tribute Band",
            "duration_ms": 278000,
            "explicit": True,
        },
    )


def test_a_different_length_is_rejected() -> None:
    assert "different length" in _judge(
        {
            "title": "Like Him (feat. Lola Young)",
            "artist": "Tyler, The Creator",
            "duration_ms": 95000,
            "explicit": True,
        },
    )


# --- what has to survive ----------------------------------------------------


def test_a_remaster_of_the_same_performance_is_accepted() -> None:
    """Is It a Crime (GBBBM8500014) is served as GBARL1100322 by a provider
    that carries the remaster. Different ISRC, same recording — rejecting it
    would fail a download that is perfectly correct.
    """
    assert (
        _judge(
            {
                "title": "Is It a Crime (Remastered)",
                "artist": "Sade",
                "duration_ms": 382000,
                "explicit": False,
            },
            expected={
                "title": "Is It a Crime",
                "artist": "Sade",
                "duration_ms": 382000,
                "explicit": False,
            },
        )
        == ""
    )


def test_an_explicit_answer_to_a_clean_request_is_accepted() -> None:
    """Only the explicit → clean direction is a downgrade. The reverse is a
    catalogue that flags more than Spotify does, not a wrong recording.
    """
    assert (
        _judge(
            {
                "title": "Like Him (feat. Lola Young)",
                "artist": "Tyler, The Creator",
                "duration_ms": 278000,
                "explicit": True,
            },
            expected={"explicit": False},
        )
        == ""
    )


# --- the lookup wrapper -----------------------------------------------------


def _reason(monkeypatch, found, **kwargs):
    async def fake_lookup(isrc):
        return found

    monkeypatch.setattr(recording_guard, "_lookup_isrc_async", fake_lookup)
    call = {
        "requested_isrc": "USQX92405794",
        "resolved_isrc": "QZYD92602058",
        "title": LIKE_HIM["title"],
        "artist": LIKE_HIM["artist"],
        "duration_ms": LIKE_HIM["duration_ms"],
        "is_explicit": LIKE_HIM["explicit"],
        **kwargs,
    }
    return asyncio.run(wrong_recording_reason_async(**call))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resolved_isrc": "USQX92405794"},  # the provider agreed
        {"resolved_isrc": "usqx-924-05794"},  # …in a different notation
        {"resolved_isrc": ""},  # it said nothing
        {"requested_isrc": ""},  # nothing to contradict
    ],
)
def test_no_drift_costs_no_lookup(monkeypatch, kwargs) -> None:
    """The common case is every download that went right, so it must not
    reach the network at all.
    """

    async def explode(isrc):
        raise AssertionError(f"looked up {isrc}, which did not drift")

    monkeypatch.setattr(recording_guard, "_lookup_isrc_async", explode)
    call = {
        "requested_isrc": "USQX92405794",
        "resolved_isrc": "QZYD92602058",
        "title": LIKE_HIM["title"],
        "artist": LIKE_HIM["artist"],
        "duration_ms": LIKE_HIM["duration_ms"],
        "is_explicit": LIKE_HIM["explicit"],
        **kwargs,
    }
    assert asyncio.run(wrong_recording_reason_async(**call)) == ""


def test_an_unanswerable_lookup_leaves_the_download_alone(monkeypatch) -> None:
    """Deezer being unreachable is not evidence of anything, and must not
    turn into a failed download.
    """
    assert _reason(monkeypatch, None) == ""


def test_a_drift_to_a_karaoke_take_names_both_isrcs(monkeypatch) -> None:
    reason = _reason(
        monkeypatch,
        {
            "title": "Like Him (Melody Karaoke Version)",
            "artist": "ZZang KARAOKE",
            "duration_ms": 279000,
            "explicit": False,
        },
    )
    assert "QZYD92602058" in reason
    assert "USQX92405794" in reason

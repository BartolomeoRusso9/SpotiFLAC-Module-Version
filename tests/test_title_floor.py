"""A candidate has to agree about the *title*, not just the artist.

score_track_match() weighed the title at 0.6 and the artist at 0.4, and a
corroborating running time added 0.05 on top. That gave any track by the
right artist a floor of 0.45 before its title was considered at all — and
letter overlap between two unrelated Italian titles is worth enough on top
of that to clear an accept threshold.

Measured: iTunes does not carry Sfera Ebbasta's "Bottiglie Privè", so a
search for it answered with "Visiera A Becco" — a different song, from an
album four years earlier, running 1.4s away from the right length. It
scored 0.666 against a 0.55 threshold, and the downloaded file was tagged
with that album's cover art instead of "Famoso"'s.
"""

from __future__ import annotations

import pytest

from SpotiFLAC.core.metadata_enrichment import _APPLE_MATCH_MIN
from SpotiFLAC.core.text_match import (
    TITLE_FLOOR,
    score_track_match,
)


class _Candidate:
    """The shape score_track_match() reads off a search hit."""

    def __init__(self, title, artist="", album="", duration_ms=0):
        self.title = title
        self.artists = artist
        self.first_artist = artist
        self.album = album
        self.duration_ms = duration_ms


def _score(candidate, title="Bottiglie Privè", album="Famoso", duration_ms=189000):
    return score_track_match(
        title=title,
        artist="Sfera Ebbasta",
        album=album,
        duration_ms=duration_ms,
        candidate=candidate,
    )


# --- the reported failure -------------------------------------------------


def test_the_itunes_mismatch_that_swapped_the_cover_art():
    """The exact hit, with its real running time and album."""
    wrong = _Candidate("Visiera A Becco", "Sfera Ebbasta", "Sfera Ebbasta", 187565)
    assert _score(wrong) < _APPLE_MATCH_MIN


@pytest.mark.parametrize(
    "title, duration_ms",
    [
        ("Visiera A Becco", 187565),
        ("Figli Di Papà", 205474),
        ("Pablo", 166760),
        ("Happy Birthday", 170707),
    ],
)
def test_no_other_track_by_the_same_artist_gets_through(title, duration_ms):
    candidate = _Candidate(title, "Sfera Ebbasta", "Sfera Ebbasta", duration_ms)
    assert _score(candidate, duration_ms=189000) < _APPLE_MATCH_MIN


def test_a_perfect_artist_and_duration_cannot_carry_a_wrong_title():
    """The floor the artist term used to guarantee, on its own."""
    candidate = _Candidate("Visiera A Becco", "Sfera Ebbasta", "Famoso", 189000)
    assert _score(candidate) < _APPLE_MATCH_MIN


# --- and the matches that must survive it ---------------------------------


@pytest.mark.parametrize(
    "candidate_title",
    [
        "Bottiglie Privè",
        "Bottiglie Prive",  # accent dropped
        "BOTTIGLIE PRIVE'",  # shouted, apostrophe for the accent
        "Bottiglie Privè (Remastered)",
        "Bottiglie Privè - Live",
    ],
)
def test_real_variants_of_the_title_still_match(candidate_title):
    candidate = _Candidate(candidate_title, "Sfera Ebbasta", "Famoso", 189000)
    assert _score(candidate) >= _APPLE_MATCH_MIN


def test_a_featuring_credit_in_either_title_is_not_a_mismatch():
    ours = "Tik Tok (feat. Marracash & Guè)"
    candidate = _Candidate("Tik Tok", "Sfera Ebbasta", "Famoso", 189000)
    assert _score(candidate, title=ours) >= _APPLE_MATCH_MIN


def test_a_one_letter_difference_is_still_a_match():
    """"Gelosi" against "Gelosa" — a typo or a localisation, not another song."""
    candidate = _Candidate("Gelosa", "Sfera Ebbasta", "Famoso", 189000)
    assert _score(candidate, title="Gelosi") >= _APPLE_MATCH_MIN


def test_the_floor_leaves_a_clear_gap_around_it():
    """Genuine variants land far above it, wrong songs far below."""
    good = _Candidate("Bottiglie Privè (Remastered)", "Sfera Ebbasta", "Famoso", 189000)
    bad = _Candidate("Visiera A Becco", "Sfera Ebbasta", "Sfera Ebbasta", 187565)
    assert _score(good) > TITLE_FLOOR * 1.5
    assert _score(bad) < TITLE_FLOOR


def test_an_empty_title_is_not_penalised():
    """Nothing to disagree with, so the guard stays out of the way.

    0.4 for the artist, plus 0.05 each for the album and the running time.
    Had the penalty applied to the artist term it would read 0.3.
    """
    candidate = _Candidate("whatever", "Sfera Ebbasta", "Famoso", 189000)
    assert _score(candidate, title="") == pytest.approx(0.5)

"""What the "Fix Local Files" matcher must and must not do.

A match at or above local_matcher.SAFE_MATCH_THRESHOLD is pre-ticked in the
UI, so hitting Apply rewrites those files' tags with no per-file review. That
makes a false positive here a data-loss bug, not a ranking nuisance, and it
is why the "must not auto-apply" cases below are the important half of this
file.

The pairs are the ones that actually caught the old scorer out — it glued
"artist title" into a single string, so whichever field was longer decided
the result. See core/text_match.py.
"""

from __future__ import annotations

import pytest

from SpotiFLAC.core.local_matcher import (
    SAFE_MATCH_THRESHOLD,
    MatchCandidate,
    score_match,
)
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.core.text_match import ratio, variant_conflict


def _track(title: str, artist: str, duration_ms: int = 0) -> TrackMetadata:
    return TrackMetadata(
        id="0" * 22,
        title=title,
        artists=artist,
        album="",
        album_artist=artist,
        duration_ms=duration_ms,
    )


def _candidate(
    local_title: str,
    local_artist: str,
    cand_title: str,
    cand_artist: str,
    *,
    local_duration: int = 0,
    cand_duration: int = 0,
) -> MatchCandidate:
    """Builds a candidate exactly the way search_and_match() does, so the
    is_safe rules are exercised rather than re-implemented here.
    """
    track = _track(cand_title, cand_artist, cand_duration)
    durations_known = bool(local_duration and cand_duration)
    return MatchCandidate(
        metadata=track,
        confidence=score_match(
            local_title, local_artist, track, duration_ms=local_duration
        ),
        title_ratio=ratio(local_title, cand_title),
        variant_unconfirmed=(
            not durations_known and variant_conflict(local_title, cand_title)
        ),
        artist_known=bool(local_artist),
    )


# (local title, local artist, candidate title, candidate artist, why)
MUST_NOT_AUTO_APPLY = [
    # The regression that motivated all of this: an identical, long artist
    # name used to carry a different song to 93.8 — above the threshold.
    (
        "Can't Stop",
        "Red Hot Chili Peppers",
        "Don't Stop",
        "Red Hot Chili Peppers",
        "different song, same artist, near-identical title",
    ),
    (
        "Juicy",
        "The Notorious B.I.G.",
        "Big Poppa",
        "The Notorious B.I.G.",
        "different song by the same artist",
    ),
    ("Time", "Pink Floyd", "Money", "Pink Floyd", "different song, same album"),
    (
        "Alive",
        "Pearl Jam",
        "Alive",
        "Empire Of The Sun",
        "same title, unrelated artist",
    ),
]

# (local title, local artist, candidate title, candidate artist, why)
MUST_AUTO_APPLY = [
    ("Juicy", "The Notorious B.I.G.", "Juicy", "The Notorious B.I.G.", "exact"),
    (
        "Everlong",
        "Foo Fighters",
        "Everlong (Remastered)",
        "Foo Fighters",
        "a remaster is the same recording",
    ),
    (
        "Bohemian Rhapsody",
        "Queen",
        "Bohemian Rhapsody - Remastered 2011",
        "Queen",
        "suffixed remaster",
    ),
    (
        "Sicko Mode",
        "Travis Scott",
        "SICKO MODE",
        "Travis Scott, Drake",
        "case difference and an extra credited artist",
    ),
    ("Cafe", "Sfera Ebbasta", "Café", "Sfera Ebbasta", "the file lost the accent"),
    ("Where Is My Mind?", "Pixies", "Where Is My Mind", "Pixies", "punctuation only"),
]


@pytest.mark.parametrize(
    ("local_title", "local_artist", "cand_title", "cand_artist", "why"),
    MUST_NOT_AUTO_APPLY,
)
def test_wrong_track_is_never_auto_applied(
    local_title, local_artist, cand_title, cand_artist, why
) -> None:
    candidate = _candidate(local_title, local_artist, cand_title, cand_artist)
    assert not candidate.is_safe, (
        f"{why}: '{cand_artist} - {cand_title}' would be written over "
        f"'{local_artist} - {local_title}' unattended "
        f"(confidence {candidate.confidence}, title ratio {candidate.title_ratio:.2f})"
    )


@pytest.mark.parametrize(
    ("local_title", "local_artist", "cand_title", "cand_artist", "why"),
    MUST_AUTO_APPLY,
)
def test_right_track_is_auto_applied(
    local_title, local_artist, cand_title, cand_artist, why
) -> None:
    candidate = _candidate(local_title, local_artist, cand_title, cand_artist)
    assert candidate.is_safe, (
        f"{why}: '{cand_artist} - {cand_title}' is the same recording as "
        f"'{local_artist} - {local_title}' but scored only "
        f"{candidate.confidence} (title ratio {candidate.title_ratio:.2f})"
    )


def test_live_version_is_held_back_when_nothing_can_confirm_it() -> None:
    """strip_noise() erases " - Live", so the text alone scores this a perfect
    match. Without a duration to separate the two recordings it must not be
    applied unattended.
    """
    candidate = _candidate(
        "Otherside",
        "Red Hot Chili Peppers",
        "Otherside - Live",
        "Red Hot Chili Peppers",
    )
    assert candidate.title_ratio == pytest.approx(1.0)
    assert candidate.variant_unconfirmed
    assert not candidate.is_safe


def test_disagreeing_durations_sink_a_live_version() -> None:
    """With running times available the duration penalty does the work, and
    the variant flag is not needed.
    """
    candidate = _candidate(
        "Everlong",
        "Foo Fighters",
        "Everlong - Live",
        "Foo Fighters",
        local_duration=250_000,
        cand_duration=331_000,
    )
    assert not candidate.variant_unconfirmed  # durations were known
    assert candidate.confidence < SAFE_MATCH_THRESHOLD
    assert not candidate.is_safe


def test_agreeing_durations_confirm_a_remaster() -> None:
    candidate = _candidate(
        "Everlong",
        "Foo Fighters",
        "Everlong (Remastered)",
        "Foo Fighters",
        local_duration=250_000,
        cand_duration=250_500,
    )
    assert candidate.is_safe


def test_isrc_match_is_identity_not_similarity() -> None:
    """An ISRC names the recording, so a hit is safe even when the titles look
    nothing alike — a file tagged with the wrong title is exactly the case the
    local tagger exists to fix.
    """
    candidate = MatchCandidate(
        metadata=_track("Correct Title", "Correct Artist"),
        confidence=100.0,
        how="isrc",
        title_ratio=0.1,
    )
    assert candidate.is_safe


def test_title_only_match_is_never_auto_applied() -> None:
    """A file with no tags and a useless name ("01.mp3") is guessed into
    title="01", artist="" — and then a track that really is called "01"
    scores a perfect title match with nothing to contradict it.

    The old scorer was accidentally safe here: it compared the concatenated
    " 01" against "Some Artist 01", which scored 20. Weighting the fields
    separately removed that accident, so the guard has to be explicit.
    """
    for junk in ("01", "audio", "track01", "5f3a9c2b"):
        candidate = _candidate(junk, "", junk, "Some Artist")
        assert candidate.confidence == pytest.approx(100.0), (
            "title-only agreement is expected to score full marks; the point "
            "is that the score alone must not be what decides"
        )
        assert not candidate.artist_known
        assert not candidate.is_safe, (
            f"a file called {junk}.mp3 would have its tags overwritten with "
            f"'Some Artist - {junk}' unattended"
        )


def test_a_guessed_artist_from_the_filename_still_counts() -> None:
    """The guard is "no artist at all", not "no artist tag" — a file named
    'Foo Fighters - Everlong.flac' has no tags but is perfectly identifiable.
    """
    candidate = _candidate("Everlong", "Foo Fighters", "Everlong", "Foo Fighters")
    assert candidate.artist_known
    assert candidate.is_safe


def test_artist_alias_is_offered_but_not_auto_applied() -> None:
    """ "2Pac" and "Tupac Shakur" are the same person, and nothing in the text
    says so. The right behaviour is to surface the match for the user to
    confirm, not to guess — so it scores well short of the threshold while
    still being a plausible candidate.
    """
    candidate = _candidate("Changes", "2Pac", "Changes", "Tupac Shakur")
    assert not candidate.is_safe
    assert candidate.confidence > 50  # still worth showing


# --- artist credits: the same artist written differently --------------------


@pytest.mark.parametrize(
    ("expected", "found", "why"),
    [
        ("Drake", "Drake feat. Rihanna", "a featured artist in the credit"),
        ("Travis Scott", "Travis Scott, Drake", "an extra credited artist"),
        ("Sfera Ebbasta", "Ebbasta Sfera", "the words in another order"),
        ("Lazza, Low Kidd", "Low Kidd & Lazza", "another separator and order"),
        ("Djavan", "Đavan", "a letter NFKD does not decompose"),
        ("Coeur", "Cœur", "a ligature"),
        ("YOASOBI", "ヨアソビ", "the same artist in another script"),
    ],
)
def test_the_same_artist_written_differently_still_matches(expected, found, why):
    """Artist carries 0.4 of the score, so treating these as different
    artists drags correct matches under the threshold. Measured before the
    fix: 0.43, 0.80, 0.54, 0.57, 0.73, 0.67 and 0.00 respectively.
    """
    from SpotiFLAC.core.text_match import artist_ratio

    assert artist_ratio(expected, found) == pytest.approx(1.0), why


@pytest.mark.parametrize(
    ("expected", "found"),
    [
        ("Drake", "Kendrick Lamar"),
        ("Queen", "Pearl Jam"),
        ("50 Cent", "The Game"),
        ("Pearl Jam", "Empire Of The Sun"),
    ],
)
def test_different_artists_stay_different(expected, found) -> None:
    """The generosity above must not extend to genuinely unrelated names —
    that is what keeps "Alive" by Pearl Jam from matching "Alive" by Empire
    Of The Sun.
    """
    from SpotiFLAC.core.text_match import artist_ratio, artists_match

    assert not artists_match(expected, found)
    assert artist_ratio(expected, found) < 0.5


def test_cross_script_is_not_a_blanket_yes() -> None:
    """The cross-script rule declines to reject on absent evidence; it must
    not fire when both sides are readable as the same script.
    """
    from SpotiFLAC.core.text_match import artists_match, is_latin_script

    assert is_latin_script("Drake")
    assert not is_latin_script("ヨアソビ")
    assert not artists_match("Drake", "Kendrick Lamar")

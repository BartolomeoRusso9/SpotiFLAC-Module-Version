"""SpotiFLAC/core/text_match.py — scoring a search result against what we
already know about a track.

Two features ask the same question — "is this Spotify result the track I am
holding?" — and used to answer it with two different implementations:

  - csv_source.py, for a row of a CSV export, with weighted per-field
    ratios, decoration stripping and a duration check;
  - local_matcher.py, for a file on disk, by gluing "artist title" into one
    string and running SequenceMatcher over it.

The second is strictly worse, and measurably so. Concatenating the fields
lets whichever one is longer decide the score, which cuts both ways:

  "Red Hot Chili Peppers - Can't Stop" scored 93.8 against
  "Red Hot Chili Peppers - Don't Stop" — different song, and above the
  local tagger's auto-apply threshold, so it would have rewritten the
  file's tags unattended.

  "Foo Fighters - Everlong" scored 76.4 against
  "Foo Fighters - Everlong (Remastered)" — the same recording, rejected.

Both follow from the same root cause, so both are fixed by scoring the
fields separately and weighting them. This module is that one
implementation; csv_source and local_matcher are now both callers.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

#: Beyond this a candidate of the same name is treated as a different
#: recording (an edit, a live take, a full DJ mix) and penalised.
DURATION_TOLERANCE_MS = 7000

#: Within this the running times agree closely enough to be corroboration.
DURATION_MATCH_MS = 3000

_NON_WORD_RE = re.compile(r"\W+", re.UNICODE)

#: "(feat. X)", "[Remastered]", " - 2011 Remaster", " - Live at …"
_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*[\)\]]\s*$"
    r"|\s+-\s+(?:[^-]*\b(?:remaster|remastered|live|mix|edit|version|mono|stereo|deluxe|bonus)\b.*)$",
    re.IGNORECASE,
)


#: A credit trailing a title without brackets — "Song feat. X". The
#: bracketed forms are _TITLE_NOISE_RE's job; this catches the ones a
#: catalogue writes bare, which titles_match() has to remove before it can
#: compare two titles by equality.
_TRAILING_CREDIT_RE = re.compile(r"\s+(?:feat|ft|featuring)\.?\s+\S.*$", re.IGNORECASE)


#: Decorations that mark a *different recording* of the same song. Both these
#: and the benign ones below are stripped before comparing titles — that is
#: what lets "Everlong" match "Everlong (Remastered)" — but stripping them
#: also means the text alone can no longer tell "Otherside" from "Otherside -
#: Live", which score 1.00 against each other once stripped. So they are
#: listed separately: see variant_conflict().
_DIFFERENT_RECORDING_RE = re.compile(
    r"\b(live|remix|mix|edit|instrumental|acoustic|demo|karaoke|"
    r"a cappella|acapella|reprise|radio edit|extended|"
    # How a karaoke or tribute catalogue names itself when it does not use
    # the word "karaoke": every one of these appears in a title that is
    # otherwise identical to the original.
    r"backing track|sing[- ]?along|tribute|made famous by|"
    r"originally performed by|in the style of|"
    # And the censored edit, which is the same performance with words
    # removed — a different recording by any measure that matters here.
    r"clean version|clean edit|censored)\b",
    re.IGNORECASE,
)


def variant_conflict(local_title: str, candidate_title: str) -> bool:
    """True when the candidate looks like a different *recording* of the song.

    A remaster is the same performance and is normally what someone wants
    tagged; a live take, a remix or an instrumental is a different recording
    that happens to share a name. strip_noise() erases both distinctions, so
    this reports the ones that matter when nothing else (a duration) is
    available to separate them.
    """
    local_marked = bool(_DIFFERENT_RECORDING_RE.search(local_title or ""))
    candidate_marked = bool(_DIFFERENT_RECORDING_RE.search(candidate_title or ""))
    return candidate_marked != local_marked


#: Letters NFKD leaves alone because they are distinct letters rather than a
#: base plus a combining mark. Catalogues transliterate them inconsistently,
#: so "Đavan" and "Djavan" have to fold to the same thing.
_TRANSLITERATIONS = {
    "đ": "dj",
    "ø": "o",
    "æ": "ae",
    "œ": "oe",
    "ß": "ss",
    "þ": "th",
    "ð": "d",
    "ł": "l",
}

#: Separators a credit list can use. "x" and "and" are surrounded by spaces
#: on purpose: they are ordinary letters inside a name ("Sixx", "Anderson").
_ARTIST_SPLIT_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring|with|vs\.?|and|x)\s+|[,;&/]|\s+\+\s+",
    re.IGNORECASE,
)

#: Ranges that are definitely not Latin script. A name written in one of
#: these and its romanisation share no characters at all, so comparing them
#: as text is meaningless — see artists_match().
_NON_LATIN_RANGES = (
    (0x4E00, 0x9FFF),  # CJK
    (0x3040, 0x309F),  # hiragana
    (0x30A0, 0x30FF),  # katakana
    (0xAC00, 0xD7AF),  # hangul
    (0x0600, 0x06FF),  # arabic
    (0x0400, 0x04FF),  # cyrillic
    (0x0590, 0x05FF),  # hebrew
    (0x0E00, 0x0E7F),  # thai
)


def is_latin_script(value: str) -> bool:
    """Whether `value` is written in a Latin alphabet."""
    return not any(
        any(low <= ord(ch) <= high for low, high in _NON_LATIN_RANGES) for ch in value
    )


def fold(value: str) -> str:
    """Casefolds, strips accents and reduces punctuation to single spaces.

    Accent folding is what lets a file tagged "Cafe" match Spotify's
    "Café": casefold() alone leaves the two different, and the difference
    is never meaningful for identifying a recording.
    """
    text = unicodedata.normalize("NFKD", value or "").casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(_TRANSLITERATIONS.get(ch, ch) for ch in text)
    return _NON_WORD_RE.sub(" ", text).strip()


def strip_noise(value: str) -> str:
    """Drops the decorations two catalogues disagree about.

    "Everlong (Remastered)" and "Everlong" are the same recording as far as
    a local file is concerned; comparing the stripped forms as well as the
    full ones keeps that from costing a match.
    """
    return _TITLE_NOISE_RE.sub("", value or "").strip()


def ratio(left: str, right: str) -> float:
    """Similarity of two strings in 0…1, ignoring case, accents and the
    decorations strip_noise() removes.
    """
    left_folded, right_folded = fold(left), fold(right)
    if not left_folded or not right_folded:
        return 0.0
    if left_folded == right_folded:
        return 1.0
    plain = SequenceMatcher(None, left_folded, right_folded).ratio()
    stripped = SequenceMatcher(
        None, fold(strip_noise(left)), fold(strip_noise(right))
    ).ratio()
    return max(plain, stripped)


def split_artists(value: str) -> list[str]:
    """A credit string broken into the individual artists it names.

    "Travis Scott, Drake" and "Drake feat. Rihanna" are one string in the
    tag and two artists in fact. Comparing the strings whole is what made
    "Drake" score 0.43 against "Drake feat. Rihanna" — the same artist.
    """
    parts = (fold(part) for part in _ARTIST_SPLIT_RE.split(value or ""))
    return [part for part in parts if part]


def _same_words(left: str, right: str) -> bool:
    """Whether two names use the same words in any order.

    Catalogues disagree about ordering more often than you would expect —
    "Lazza, Low Kidd" against "Low Kidd & Lazza", or a surname-first
    rendering.
    """
    left_words, right_words = left.split(), right.split()
    return bool(left_words) and sorted(left_words) == sorted(right_words)


def artists_match(expected: str, found: str) -> bool:
    """Whether two credit strings name the same artist.

    A deliberately generous predicate, because the failure it guards
    against is rejecting a correct match: the credit lists two catalogues
    attach to one recording differ constantly in featured artists,
    separators and ordering, and none of that means the recording is
    different.

    The last rule is the unintuitive one. When one side is written in a
    non-Latin script and the other is not, they share no characters at all
    — "YOASOBI" against "ヨアソビ" scores exactly 0.00 — so text comparison
    says nothing whatsoever. Treating that as a match is not a guess: it is
    declining to reject on evidence that does not exist, and leaving the
    decision to the title and duration.
    """
    expected_folded, found_folded = fold(expected), fold(found)
    if not expected_folded or not found_folded:
        return False
    if expected_folded == found_folded:
        return True
    if expected_folded in found_folded or found_folded in expected_folded:
        return True
    if _same_words(expected_folded, found_folded):
        return True

    for one in split_artists(expected):
        for other in split_artists(found):
            if one == other or one in other or other in one:
                return True
            if _same_words(one, other):
                return True

    return is_latin_script(expected) != is_latin_script(found)


def artist_ratio(expected: str, found: str) -> float:
    """Similarity of two credit strings in 0…1, artist-aware.

    1.0 whenever artists_match() is satisfied, so a featured artist or a
    different ordering costs nothing; otherwise the best similarity between
    any pair of the individual names, falling back to the whole strings.
    """
    if artists_match(expected, found):
        return 1.0
    best = ratio(expected, found)
    for one in split_artists(expected):
        for other in split_artists(found):
            best = max(best, SequenceMatcher(None, one, other).ratio())
    return best


def _decorations_stripped(value: str) -> str:
    """`value` with every recognised decoration removed, not just the last.

    strip_noise() peels one trailing group, so "Everlong (Remastered) [Live]"
    still comes back decorated. titles_match() compares the results by
    equality, so the peeling has to run to a fixed point or the equality
    never lands on titles carrying two of them.
    """
    text = (value or "").strip()
    for _ in range(4):
        stripped = _TRAILING_CREDIT_RE.sub("", strip_noise(text)).strip()
        if not stripped or stripped == text:
            break
        text = stripped
    return text


def titles_match(expected: str, found: str) -> bool:
    """Whether two titles name the same song.

    Generous about decorations, strict about everything else. Catalogues
    disagree over parenthesised suffixes, featured-artist credits and
    punctuation, and none of that makes it a different song — so those are
    stripped from both sides first, and what is left has to be *equal*.

    Plain containment used to stand in for that, and it is not the same
    test: "Love" is contained in "Love Story", "Alone" in "Not Alone", "Run"
    in "Run the World". Every one of those is a different song, and every
    one of them satisfied a substring check — which is how a provider that
    returned the wrong recording got past track_identity_mismatch(), the one
    check that exists to catch it. The ratio floor below still covers a
    decoration nothing here recognises.
    """
    expected_folded, found_folded = fold(expected), fold(found)
    if not expected_folded or not found_folded:
        return False
    if expected_folded == found_folded:
        return True

    expected_core = fold(_decorations_stripped(expected))
    found_core = fold(_decorations_stripped(found))
    if expected_core and found_core and expected_core == found_core:
        return True

    return ratio(expected, found) >= 0.9


def track_identity_mismatch(
    *,
    expected_title: str = "",
    expected_artist: str = "",
    expected_album: str = "",
    expected_isrc: str = "",
    found_title: str = "",
    found_artist: str = "",
    found_album: str = "",
    found_isrc: str = "",
) -> str:
    """Why the track that came back is not the one asked for, or "".

    A provider that resolves the wrong recording — a cover, a re-recording,
    a different artist with the same song title — otherwise produces a file
    of one track carrying another track's tags, permanently and silently.
    This is the check that catches it.

    Every comparison is skipped when either side is empty: an extension that
    reports nothing about what it fetched cannot be contradicted, and
    rejecting on missing information would break every provider that stays
    quiet. Only positive disagreement rejects.

    A matching ISRC settles it outright — it names the recording, so the
    titles may say whatever they like. A *conflicting* ISRC is the opposite:
    strong evidence of the wrong track, and it removes the album leniency
    below.
    """
    from .isrc_utils import normalize_isrc

    want_isrc = normalize_isrc(expected_isrc)
    got_isrc = normalize_isrc(found_isrc)
    if want_isrc and got_isrc:
        if want_isrc == got_isrc:
            return ""
        return f"different recording: asked for ISRC {want_isrc}, got {got_isrc}"

    # Before the title comparison, because strip_noise() erases exactly the
    # decoration that separates these: "Like Him (feat. Lola Young)" and
    # "Like Him (feat. Lola Young) (Melody Karaoke Version)" fold to the same
    # string, and titles_match() then calls them the same song. They are the
    # same *song* — and a different recording of it, which is the thing this
    # function exists to catch.
    if expected_title and found_title and variant_conflict(expected_title, found_title):
        return f"different version: asked for {expected_title!r}, got {found_title!r}"

    if (
        expected_artist
        and found_artist
        and not artists_match(expected_artist, found_artist)
    ):
        return f"different artist: asked for {expected_artist!r}, got {found_artist!r}"

    if expected_title and found_title and not titles_match(expected_title, found_title):
        return f"different title: asked for {expected_title!r}, got {found_title!r}"

    # Album last, and forgivingly: the same recording legitimately appears on
    # a single, an album, a deluxe edition and three compilations, so a
    # disagreement here is only meaningful when the track identity is not
    # already established by title and artist.
    if expected_album and found_album and not titles_match(expected_album, found_album):
        strong_identity = bool(
            expected_title
            and found_title
            and fold(expected_title) == fold(found_title)
            and expected_artist
            and found_artist
            and artists_match(expected_artist, found_artist)
        )
        if not strong_identity:
            return f"different album: asked for {expected_album!r}, got {found_album!r}"

    return ""


def score_track_match(
    *,
    title: str,
    artist: str = "",
    album: str = "",
    duration_ms: int = 0,
    candidate,
) -> float:
    """How well a search result answers what we know about a track, in 0…1.

    Title carries the most weight and artist the rest, scored *separately*
    so neither can mask the other — a wrong title cannot ride in on a long
    matching artist name, and an artist written under a different alias
    ("2Pac" / "Tupac Shakur") cannot sink a title that agrees exactly.

    Album and duration only adjust the result, because a source that has
    them is not necessarily a source that agrees with Spotify about them (a
    single vs. its album, a remaster's running time). A duration out by more
    than `DURATION_TOLERANCE_MS` is the one signal strong enough to actively
    push a candidate down: same name, different recording.
    """
    title_score = ratio(title, getattr(candidate, "title", ""))
    artists = getattr(candidate, "artists", "") or ""
    first_artist = getattr(candidate, "first_artist", "") or ""

    if artist:
        artist_score = max(
            artist_ratio(artist, artists), artist_ratio(artist, first_artist)
        )
        score = 0.6 * title_score + 0.4 * artist_score
    else:
        score = title_score

    if album and getattr(candidate, "album", ""):
        score = min(1.0, score + 0.05 * ratio(album, candidate.album))

    candidate_duration = int(getattr(candidate, "duration_ms", 0) or 0)
    if duration_ms and candidate_duration:
        delta = abs(duration_ms - candidate_duration)
        if delta <= DURATION_MATCH_MS:
            score = min(1.0, score + 0.05)
        elif delta > DURATION_TOLERANCE_MS:
            score *= 0.7

    return round(score, 4)

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


#: Decorations that mark a *different recording* of the same song. Both these
#: and the benign ones below are stripped before comparing titles — that is
#: what lets "Everlong" match "Everlong (Remastered)" — but stripping them
#: also means the text alone can no longer tell "Otherside" from "Otherside -
#: Live", which score 1.00 against each other once stripped. So they are
#: listed separately: see variant_conflict().
_DIFFERENT_RECORDING_RE = re.compile(
    r"\b(live|remix|mix|edit|instrumental|acoustic|demo|karaoke|"
    r"a cappella|acapella|reprise|radio edit|extended)\b",
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


def fold(value: str) -> str:
    """Casefolds, strips accents and reduces punctuation to single spaces.

    Accent folding is what lets a file tagged "Cafe" match Spotify's
    "Café": casefold() alone leaves the two different, and the difference
    is never meaningful for identifying a recording.
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _NON_WORD_RE.sub(" ", text.casefold()).strip()


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
        artist_score = max(ratio(artist, artists), ratio(artist, first_artist))
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

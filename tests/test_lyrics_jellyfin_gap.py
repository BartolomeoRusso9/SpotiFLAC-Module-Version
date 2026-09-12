"""Word-by-word lyrics have to survive Jellyfin's per-cue Trim().

Jellyfin extracts embedded lyrics during a library scan, parses them into one
cue per timed word, runs .NET's string.Trim() over each cue's text and writes
the result back to its metadata cache. The trim eats the space that separated
a word from the next, so "Nuij simm doije" reaches every client — Jellify,
Finamp, the web player — as "Nuijsimmdoije".

SPOTIFLAC_LYRICS_JELLYFIN_COMPAT swaps that separator for U+2800 BRAILLE
PATTERN BLANK, which draws as a blank but is category So rather than Zs, so
char.IsWhiteSpace is false for it and Trim() leaves it alone.
"""

from __future__ import annotations

import re
import unicodedata

from SpotiFLAC.core.lyrics import JELLYFIN_WORD_GAP, apply_jellyfin_word_gap

APPLE_LRC = "[00:08.61]<00:08.61>Nuij <00:08.91>simm <00:09.44>doije <00:10.41>stann'"

_CUE = re.compile(r"<(\d{2}:\d{2}\.\d{2})>([^<]*)")


def _as_jellyfin_renders(lrc_line: str) -> str:
    """The line as Jellyfin serves it: one cue per word, each one trimmed."""
    return "".join(text.strip() for _, text in _CUE.findall(lrc_line))


def test_gap_character_is_not_whitespace() -> None:
    # The whole trick rests on this: Zs would be trimmed, So is not.
    assert unicodedata.category(JELLYFIN_WORD_GAP) == "So"
    assert not JELLYFIN_WORD_GAP.isspace()


def test_plain_apple_lrc_loses_its_word_breaks() -> None:
    assert _as_jellyfin_renders(APPLE_LRC) == "Nuijsimmdoijestann'"


def test_gapped_lrc_keeps_its_word_breaks() -> None:
    gapped = _as_jellyfin_renders(apply_jellyfin_word_gap(APPLE_LRC))
    assert gapped == JELLYFIN_WORD_GAP.join(["Nuij", "simm", "doije", "stann'"])


def test_space_inside_a_timed_word_is_left_alone() -> None:
    # A trim never reaches an interior space, so there is nothing to protect
    # and rewriting it would only corrupt the text.
    out = apply_jellyfin_word_gap("[00:15.17]<00:15.17>ca <00:15.34>t'he 'a")
    assert out == f"[00:15.17]<00:15.17>ca{JELLYFIN_WORD_GAP}<00:15.34>t'he 'a"


def test_line_synced_lrc_is_untouched() -> None:
    # No inline tags means no per-word cues, so Jellyfin trims only the ends.
    plain = "[00:02.64]Damn, every time"
    assert apply_jellyfin_word_gap(plain) == plain


def test_applying_twice_changes_nothing() -> None:
    once = apply_jellyfin_word_gap(APPLE_LRC)
    assert apply_jellyfin_word_gap(once) == once


def test_a_half_converted_text_is_finished_rather_than_skipped() -> None:
    # Lyrics stitched from two sources — one line already converted, one not.
    # Stopping at the first gap anywhere would leave the second line broken.
    mixed = (
        f"[00:08.61]<00:08.61>Nuij{JELLYFIN_WORD_GAP}<00:08.91>simm\n"
        "[00:12.47]<00:12.47>T' <00:12.76>stai"
    )
    out = apply_jellyfin_word_gap(mixed)

    assert _as_jellyfin_renders(out.split("\n")[0]) == f"Nuij{JELLYFIN_WORD_GAP}simm"
    assert _as_jellyfin_renders(out.split("\n")[1]) == f"T'{JELLYFIN_WORD_GAP}stai"
    # The line that was already converted keeps exactly one gap per word.
    assert JELLYFIN_WORD_GAP * 2 not in out


def test_a_word_ending_in_gap_and_space_gains_no_second_gap() -> None:
    already = f"[00:08.61]<00:08.61>Nuij{JELLYFIN_WORD_GAP} <00:08.91>simm"
    assert JELLYFIN_WORD_GAP * 2 not in apply_jellyfin_word_gap(already)

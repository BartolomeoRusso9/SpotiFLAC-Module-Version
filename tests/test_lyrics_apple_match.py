"""Apple lyrics must come from the same song, not one that shares a word.

The iTunes search that picks which Apple song to take lyrics from accepted a
result that matched on the title alone or on the artist alone. The results
below are the real ones iTunes returned for three tracks whose embedded
lyrics turned out to belong to another song.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import lyrics
from SpotiFLAC.core.lyrics import _itunes_result_matches


def _res(title: str, artist: str, seconds: float, track_id: int = 1) -> dict:
    return {
        "trackName": title,
        "artistName": artist,
        "trackTimeMillis": int(seconds * 1000),
        "trackId": track_id,
    }


# What iTunes answered for "Valentino Vale Lambo" (the track is 180s).
VALENTINO_RESULTS = [
    _res("San Valentino", "Gigi D'Alessio & Vale Lambo", 238, 1),
    _res("Valentino (Intro)", "Cazulee", 129, 2),
    _res("Cuando Quieras (feat. Valentino)", "Nicky Jam", 228, 3),
    _res("Valentino", "JOLENE BLOOM", 200, 4),
    _res("Valentino", "Psy.P", 173, 5),
]


@pytest.mark.parametrize("res", VALENTINO_RESULTS, ids=lambda r: r["trackName"])
def test_no_other_valentino_is_taken_for_vale_lambo(res) -> None:
    assert not _itunes_result_matches(res, "Valentino", "Vale Lambo", 180)


def test_the_title_alone_is_not_enough() -> None:
    gnr = _res("November Rain", "Guns N' Roses", 536)
    assert not _itunes_result_matches(gnr, "November Rain", "Kris Wu", 194)


def test_the_artist_alone_is_not_enough() -> None:
    """ "Quanto Manca" matched a song off the album "Quanto Manca 2"."""
    other_song = _res("Lontano da Milano", "VillaBanks", 120)
    assert not _itunes_result_matches(other_song, "Quanto Manca", "VillaBanks", 263)


def test_the_right_song_is_still_accepted() -> None:
    right = _res("Valentino (feat. Geolier)", "Vale Lambo", 180.4)
    assert _itunes_result_matches(right, "Valentino", "Vale Lambo", 180)


def test_a_joint_credit_still_counts_as_the_artist() -> None:
    right = _res("Valentino", "Vale Lambo & Geolier", 181)
    assert _itunes_result_matches(right, "Valentino", "Vale Lambo", 180)


def test_punctuation_in_a_credit_does_not_hide_the_artist() -> None:
    """Regression: the first version of the check missed "21 Savage," —
    the comma after the name stopped the whole-word match."""
    res = _res("Darth Vader", "21 Savage, Offset & Metro Boomin", 229)
    assert _itunes_result_matches(res, "Darth Vader", "21 Savage", 229)


def test_an_exact_title_with_a_very_different_length_is_rejected() -> None:
    res = _res("Valentino", "Vale Lambo", 230)
    assert not _itunes_result_matches(res, "Valentino", "Vale Lambo", 180)


def test_without_a_length_only_an_exact_title_is_trusted() -> None:
    assert _itunes_result_matches(
        _res("Valentino", "Vale Lambo", 0), "Valentino", "Vale Lambo", 0
    )
    assert not _itunes_result_matches(
        _res("San Valentino", "Vale Lambo", 0), "Valentino", "Vale Lambo", 0
    )


def test_artist_containment_is_by_whole_words() -> None:
    res = _res("Sucker", "Jonas Brothers", 181)
    assert not _itunes_result_matches(res, "Sucker", "Nas", 181)


# --- the fetch itself ------------------------------------------------------


class _Response:
    is_success = True

    def __init__(self, results):
        self._results = results

    def json(self):
        return {"results": self._results}


class _Client:
    def __init__(self, results):
        self._results = results

    async def get(self, url, **kwargs):
        return _Response(self._results)


def _fetch(monkeypatch, results) -> tuple[str, list]:
    asked: list = []

    async def _client():
        return _Client(results)

    async def _ttml(song_id, word_by_word=True):
        asked.append(song_id)
        return f"[00:01.00]lyrics of {song_id}"

    monkeypatch.setattr(lyrics.NetworkManager, "get_async_client_safe", _client)
    monkeypatch.setattr(lyrics, "_fetch_apple_ttml_async", _ttml)
    text = asyncio.run(lyrics._fetch_apple_async("Valentino", "Vale Lambo", 180))
    return text, asked


def test_no_lyrics_rather_than_another_song_s(monkeypatch) -> None:
    text, asked = _fetch(monkeypatch, VALENTINO_RESULTS)
    assert text == ""
    assert asked == []


def test_the_matching_result_is_the_one_fetched(monkeypatch) -> None:
    results = [*VALENTINO_RESULTS, _res("Valentino", "Vale Lambo", 180, 99)]
    text, asked = _fetch(monkeypatch, results)
    assert asked == ["99"]
    assert "lyrics of 99" in text

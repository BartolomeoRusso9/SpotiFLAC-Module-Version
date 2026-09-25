"""BiniLyrics and Unison: Apple Music's TTML from catalogues that need no key.

Both search by name, so a result is only worth taking when it is the same
recording — by ISRC, or by a length close enough that the words fall on the
same beat. Both answer in TTML, which `apple_ttml` turns into LRC.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import lyrics as L

TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml"'
    ' xmlns:itunes="http://music.apple.com/lyric-ttml-internal"'
    ' itunes:timing="Word"><body><div>'
    '<p begin="00:02.640" end="00:04.000" itunes:key="L1">'
    '<span begin="00:02.640" end="00:03.240">Damn, </span>'
    '<span begin="00:03.240" end="00:03.640">every </span>'
    '<span begin="00:03.640" end="00:04.000">time</span>'
    "</p></div></body></tt>"
)
WORD_LRC = "[00:02.64]<00:02.64>Damn, <00:03.24>every <00:03.64>time"
LINE_LRC = "[00:02.64]Damn, every time"

BINI_URL = "https://lyrics-storage.binimum.org/GB0000000001.ttml"


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """A client that answers by address and remembers what it was asked."""

    def __init__(self, answers: dict[str, _Response]):
        self._answers = answers
        self.asked: list[str] = []

    async def get(self, url, **kwargs):
        self.asked.append(url)
        return self._answers.get(url, _Response(404))


def _use(monkeypatch, answers) -> _Client:
    client = _Client(answers)

    async def _get():
        return client

    monkeypatch.setattr(L.NetworkManager, "get_async_client_safe", _get)
    return client


def _result(**over):
    item = {
        "isrc": "GB0000000001",
        "duration": 355,
        "timing_type": "word",
        "lyricsUrl": BINI_URL,
    }
    item.update(over)
    return item


# --- which BiniLyrics result is taken -------------------------------------


def test_word_timing_beats_line_timing() -> None:
    line = _result(timing_type="line", lyricsUrl="https://x.binimum.org/line.ttml")
    word = _result(lyricsUrl="https://x.binimum.org/word.ttml")
    result = L._best_bini_result([line, word], 355, "")
    assert result is not None
    assert result["lyricsUrl"].endswith("word.ttml")


def test_a_different_length_is_a_different_take() -> None:
    assert L._best_bini_result([_result(duration=380)], 355, "") is None


def test_a_length_within_the_slack_is_the_same_take() -> None:
    assert L._best_bini_result([_result(duration=358)], 355, "") is not None


def test_a_matching_isrc_is_taken_whatever_the_length() -> None:
    assert L._best_bini_result([_result(duration=380)], 355, "gb0000000001") is not None


def test_the_closest_length_wins_among_equals() -> None:
    far = _result(duration=358, lyricsUrl="https://x.binimum.org/far.ttml")
    near = _result(duration=356, lyricsUrl="https://x.binimum.org/near.ttml")
    result = L._best_bini_result([far, near], 355, "")
    assert result is not None
    assert result["lyricsUrl"].endswith("near.ttml")


def test_a_link_off_binis_own_host_is_never_taken() -> None:
    for url in (
        "http://lyrics-storage.binimum.org/a.ttml",
        "https://evil.example/a.ttml",
        "https://binimum.org.evil.example/a.ttml",
        None,
    ):
        assert L._best_bini_result([_result(lyricsUrl=url)], 355, "") is None


# --- BiniLyrics, end to end -----------------------------------------------


def _bini(monkeypatch, results, **kwargs) -> tuple[str, _Client]:
    client = _use(
        monkeypatch,
        {
            L._BINI_API: _Response(payload={"results": results}),
            BINI_URL: _Response(text=TTML),
        },
    )
    text = asyncio.run(
        L._fetch_bini_async(
            "Like Him", "Tyler, The Creator", "Chromakopia", 355, "", **kwargs
        )
    )
    return text, client


def test_bini_gives_the_word_timed_lrc(monkeypatch) -> None:
    text, client = _bini(monkeypatch, [_result()])
    assert text == WORD_LRC
    assert client.asked == [L._BINI_API, BINI_URL]


def test_bini_gives_plain_lines_when_words_are_not_wanted(monkeypatch) -> None:
    text, _ = _bini(monkeypatch, [_result()], word_by_word=False)
    assert text == LINE_LRC


def test_bini_with_nothing_for_the_track_gives_nothing(monkeypatch) -> None:
    text, client = _bini(monkeypatch, [])
    assert text == ""
    assert client.asked == [L._BINI_API]


def test_bini_never_follows_a_link_off_its_own_host(monkeypatch) -> None:
    text, client = _bini(monkeypatch, [_result(lyricsUrl="https://evil.example/a")])
    assert text == ""
    assert client.asked == [L._BINI_API]


def test_bini_needs_a_name_to_search_by(monkeypatch) -> None:
    client = _use(monkeypatch, {})
    assert asyncio.run(L._fetch_bini_async("", "Queen")) == ""
    assert client.asked == []


# --- Unison ---------------------------------------------------------------


def _unison(monkeypatch, response, duration=285, isrc="", **kwargs) -> str:
    _use(monkeypatch, {L._UNISON_API: response})
    return asyncio.run(
        L._fetch_unison_async(
            "Like Him", "Tyler, The Creator", "", duration, isrc, **kwargs
        )
    )


def _hit(**data):
    body = {"isrc": "GB0000000001", "format": "ttml", "lyrics": TTML}
    body.update(data)
    return _Response(payload={"success": True, "data": body})


def test_unison_gives_the_word_timed_lrc(monkeypatch) -> None:
    assert _unison(monkeypatch, _hit(duration=285)) == WORD_LRC


def test_unison_gives_plain_lines_when_words_are_not_wanted(monkeypatch) -> None:
    assert _unison(monkeypatch, _hit(duration=285), word_by_word=False) == LINE_LRC


def test_unison_lrc_is_passed_through(monkeypatch) -> None:
    hit = _hit(format="lrc", lyrics="[00:01.00]hello", duration=285)
    assert _unison(monkeypatch, hit) == "[00:01.00]hello"


def test_unison_plain_text_is_left_to_the_other_providers(monkeypatch) -> None:
    assert (
        _unison(monkeypatch, _hit(format="plain", lyrics="hello", duration=285)) == ""
    )


def test_unison_not_found_is_nothing(monkeypatch) -> None:
    assert _unison(monkeypatch, _Response(404, {"success": False})) == ""


def test_unison_a_different_length_is_a_different_take(monkeypatch) -> None:
    assert _unison(monkeypatch, _hit(duration=320)) == ""


def test_unison_a_matching_isrc_outweighs_the_length(monkeypatch) -> None:
    hit = _hit(duration=320)
    assert _unison(monkeypatch, hit, isrc="GB0000000001") == WORD_LRC


def test_unison_without_a_length_of_its_own_is_not_taken(monkeypatch) -> None:
    assert _unison(monkeypatch, _hit()) == ""


# --- the setting is part of the cache key ---------------------------------


@pytest.mark.parametrize("provider", ["binilyrics", "unison"])
def test_word_and_line_renderings_do_not_share_a_cache_entry(
    monkeypatch, provider
) -> None:
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        L, "get_cached_response", lambda ns, key, ttl: store.get((ns, key))
    )
    monkeypatch.setattr(
        L, "put_cached_response", lambda ns, key, v: store.update({(ns, key): v})
    )

    async def fetch(ctx):
        return WORD_LRC if ctx.apple_word_by_word else LINE_LRC

    monkeypatch.setattr(L, "_PROVIDER_MAP", {provider: fetch})

    def run(word_by_word):
        return asyncio.run(
            L.fetch_lyrics_async(
                "Like Him",
                "Tyler, The Creator",
                duration_s=278,
                providers=[provider],
                apple_word_by_word=word_by_word,
            )
        )[0]

    assert "<00:03.24>" in run(True)
    assert "<00:03.24>" not in run(False)

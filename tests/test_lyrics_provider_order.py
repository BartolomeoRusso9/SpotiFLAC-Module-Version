"""`lyrics_providers` is a ranking, not a set.

Every provider is queried at once, which is what makes the lookup fast, but
the results used to be read with asyncio.as_completed() — so the fastest
answer won and the configured order decided nothing. lrclib answers in about
a tenth of a second against Apple's second-and-a-bit (an iTunes search, then
the lyrics fetch), so "apple, lrclib" always produced lrclib: line-level LRC
where the whole point of putting Apple first is its word-by-word timing.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import lyrics as L

# Word-level timings inside the line — what Apple gives and lrclib does not.
APPLE_LRC = "[00:02.64]<00:02.64>Damn, <00:03.24>every <00:03.64>time"
LRCLIB_LRC = "[00:02.64]Damn, every time"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch, tmp_path):
    """The disk cache would answer the second call from the first."""
    monkeypatch.setattr(L, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(L, "put_cached_response", lambda *a, **k: None)


def _providers(monkeypatch, answers: dict[str, tuple[float, str]]):
    """Replace the provider map with fakes: name → (delay, lyrics)."""

    def make(delay: float, text: str):
        async def fetch(_ctx):
            await asyncio.sleep(delay)
            return text

        return fetch

    monkeypatch.setattr(
        L,
        "_PROVIDER_MAP",
        {name: make(*spec) for name, spec in answers.items()},
    )


def _fetch(order):
    return asyncio.run(
        L.fetch_lyrics_async(
            "Like Him",
            "Tyler, The Creator",
            duration_s=278,
            providers=order,
        ),
    )


def test_the_slower_first_choice_still_wins(monkeypatch) -> None:
    _providers(
        monkeypatch,
        {"apple": (0.05, APPLE_LRC), "lrclib": (0.0, LRCLIB_LRC)},
    )
    text, provider = _fetch(["apple", "lrclib"])

    assert provider == "apple"
    assert "<00:03.24>" in text


def test_reversing_the_order_reverses_the_winner(monkeypatch) -> None:
    _providers(
        monkeypatch,
        {"apple": (0.0, APPLE_LRC), "lrclib": (0.05, LRCLIB_LRC)},
    )
    assert _fetch(["lrclib", "apple"])[1] == "lrclib"


def test_an_empty_first_choice_falls_through_in_order(monkeypatch) -> None:
    _providers(
        monkeypatch,
        {
            "apple": (0.0, ""),
            "musixmatch": (0.05, "[00:01.00]from musixmatch"),
            "lrclib": (0.0, LRCLIB_LRC),
        },
    )
    assert _fetch(["apple", "musixmatch", "lrclib"])[1] == "musixmatch"


def test_a_provider_that_raises_is_skipped_not_fatal(monkeypatch) -> None:
    async def explode(_ctx):
        raise RuntimeError("provider down")

    _providers(monkeypatch, {"lrclib": (0.0, LRCLIB_LRC)})
    L._PROVIDER_MAP["apple"] = explode

    assert _fetch(["apple", "lrclib"])[1] == "lrclib"


def test_nothing_anywhere_returns_nothing(monkeypatch) -> None:
    _providers(monkeypatch, {"apple": (0.0, ""), "lrclib": (0.0, "   ")})
    assert _fetch(["apple", "lrclib"]) == ("", "")


# --- the Apple word-by-word conversion --------------------------------------


def test_apple_syllables_become_inline_timestamps() -> None:
    """Apple times each syllable; `part: true` marks one that continues the
    word before it, and must not be given a space of its own.
    """
    payload = {
        "content": [
            {
                "timestamp": 16570,
                "text": [
                    {"timestamp": 16570, "text": "She"},
                    {"timestamp": 16890, "text": "said"},
                    {"timestamp": 17720, "text": "make"},
                    {"timestamp": 18040, "text": "ex", "part": True},
                    {"timestamp": 18430, "text": "pres"},
                ],
            },
        ],
    }
    line = L._apple_payload_to_lrc(payload)
    assert line == (
        "[00:16.57]<00:16.57>She <00:16.89>said <00:17.72>make"
        "<00:18.04>ex <00:18.43>pres"
    )

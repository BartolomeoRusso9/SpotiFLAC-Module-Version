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
def _no_cache(monkeypatch):
    """The disk cache would answer the second call from the first."""
    monkeypatch.setattr(L, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(L, "put_cached_response", lambda *a, **k: None)


@pytest.fixture
def cache(monkeypatch):
    """A fake response cache that honours the TTL it is asked for.

    Entries are (namespace, key) → (value, age_in_seconds), so a test can
    say "this was cached eight hours ago" without waiting.
    """
    store: dict[tuple[str, str], tuple[object, float]] = {}

    def get(namespace, key, ttl):
        entry = store.get((namespace, key))
        if entry is None or entry[1] > ttl:
            return None
        return entry[0]

    def put(namespace, key, value):
        store[(namespace, key)] = (value, 0.0)

    monkeypatch.setattr(L, "get_cached_response", get)
    monkeypatch.setattr(L, "put_cached_response", put)
    return store


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


def test_apple_line_synced_drops_the_syllable_timings() -> None:
    """With word_by_word off, the inline <mm:ss.xx> tags go away and the line
    is emitted as plain line-synced LRC — `part: true` still joins without a
    space.
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
    line = L._apple_payload_to_lrc(payload, word_by_word=False)
    assert line == "[00:16.57]She said makeex pres"


def test_apple_word_by_word_setting_reaches_the_fetcher(monkeypatch, cache) -> None:
    seen: dict[str, bool] = {}

    async def fake_apple(_ctx):
        seen["wbw"] = _ctx.apple_word_by_word
        return APPLE_LRC

    monkeypatch.setattr(L, "_PROVIDER_MAP", {"apple": fake_apple})

    asyncio.run(
        L.fetch_lyrics_async(
            "Like Him",
            "Tyler, The Creator",
            duration_s=278,
            providers=["apple"],
            apple_word_by_word=False,
        ),
    )
    assert seen["wbw"] is False
    # cached under the line-synced key, not the default word-by-word one
    assert cache[("lyrics-provider", "apple|Like Him|Tyler, The Creator||278|||line")]


# --- what the cache is allowed to remember ----------------------------------
#
# It used to remember the finished decision, keyed on the provider list — so
# when the ordering above was fixed, 65 of one playlist's 66 tracks were
# still answered with the lrclib text the old behaviour had chosen the night
# before, and the fix looked inert for a week. Each provider's own answer is
# cached now; the choosing happens fresh.


def _key(provider: str) -> tuple[str, str]:
    # Apple's key carries the word-by-word / line-synced mode ("wbw" by
    # default) — its two renderings can't share a cache entry.
    suffix = "|wbw" if provider == "apple" else ""
    return (
        "lyrics-provider",
        f"{provider}|Like Him|Tyler, The Creator||278||{suffix}",
    )


def test_each_provider_is_cached_under_its_own_name(monkeypatch, cache) -> None:
    _providers(
        monkeypatch,
        {"apple": (0.05, APPLE_LRC), "lrclib": (0.0, LRCLIB_LRC)},
    )
    _fetch(["apple", "lrclib"])

    assert cache[_key("apple")][0] == APPLE_LRC
    assert cache[_key("lrclib")][0] == LRCLIB_LRC


def test_a_cached_lower_choice_cannot_outrank_a_live_higher_one(
    monkeypatch, cache
) -> None:
    """The exact shape of the bug: lrclib already in the cache, Apple not."""
    cache[_key("lrclib")] = (LRCLIB_LRC, 0.0)
    _providers(monkeypatch, {"apple": (0.0, APPLE_LRC), "lrclib": (0.0, LRCLIB_LRC)})

    text, provider = _fetch(["apple", "lrclib"])

    assert provider == "apple"
    assert "<00:03.24>" in text


def test_a_cached_answer_is_not_fetched_again(monkeypatch, cache) -> None:
    cache[_key("apple")] = (APPLE_LRC, 0.0)

    async def explode(_ctx):
        raise AssertionError("went to the network for a cached provider")

    monkeypatch.setattr(L, "_PROVIDER_MAP", {"apple": explode})

    assert _fetch(["apple"])[1] == "apple"


def test_a_recent_miss_is_remembered(monkeypatch, cache) -> None:
    """A first choice with nothing for a track must not be re-asked on every
    track of every run.
    """
    cache[_key("apple")] = ("", 60.0)
    _providers(monkeypatch, {"apple": (0.0, APPLE_LRC), "lrclib": (0.0, LRCLIB_LRC)})

    assert _fetch(["apple", "lrclib"])[1] == "lrclib"


def test_a_stale_miss_is_asked_again(monkeypatch, cache) -> None:
    """…but only for a few hours. A catalogue that gains the lyrics later is
    noticed the same day, where a hit stays good for the full week.
    """
    cache[_key("apple")] = ("", L._LYRICS_MISS_CACHE_TTL + 60)
    _providers(monkeypatch, {"apple": (0.0, APPLE_LRC), "lrclib": (0.0, LRCLIB_LRC)})

    assert _fetch(["apple", "lrclib"])[1] == "apple"


def test_a_week_old_hit_is_still_a_hit(monkeypatch, cache) -> None:
    cache[_key("apple")] = (APPLE_LRC, L._LYRICS_MISS_CACHE_TTL + 60)
    _providers(monkeypatch, {"apple": (0.0, "fresh"), "lrclib": (0.0, LRCLIB_LRC)})

    assert _fetch(["apple", "lrclib"])[0].endswith(APPLE_LRC)

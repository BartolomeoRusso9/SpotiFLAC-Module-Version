"""A Monochrome page that fails to load falls back to the next one.

Navigation used to happen inside browser startup, before the fallback loop:
monochrome.tf failing or timing out tore the browser down and raised, so
monochrome.samidy.com was never tried. Startup no longer navigates; every
candidate is loaded, and its failure handled, inside the fallback loop.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import signed_session_mono as mono

PRIMARY, FALLBACK = mono.MONOCHROME_PAGE_URLS


class FakeTab:
    def __init__(self, unreachable=()):
        self.unreachable = set(unreachable)
        self.visited: list[str] = []
        self.url = "about:blank"

    async def go_to(self, url):
        self.visited.append(url)
        if url in self.unreachable:
            raise TimeoutError(f"{url} timed out")
        self.url = url


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(mono, "load_monochrome_session", mono.MonochromeSessionRecord)
    monkeypatch.setattr(mono, "save_monochrome_session", lambda record: None)
    return mono._MonochromeBrowserSession()


def _solves_on(session, monkeypatch, *pages):
    solved_on: list[str] = []

    async def solve(timeout):
        solved_on.append(session._tab.url)
        if session._tab.url not in pages:
            raise Exception("no token")
        return "jwt-from-" + session._tab.url

    monkeypatch.setattr(session, "_solve_turnstile_on_page", solve)
    return solved_on


def test_browser_start_does_not_navigate(session, monkeypatch) -> None:
    tab = FakeTab()

    class FakeChrome:
        def __init__(self, options):
            pass

        async def start(self):
            return tab

        async def set_window_minimized(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(mono, "Chrome", FakeChrome)
    monkeypatch.setattr(mono, "_ensure_xvfb", lambda: None)
    # Never the real one: it pkills by profile path on the host.
    monkeypatch.setattr(mono, "_kill_by_profile_dir", lambda profile_dir: None)
    monkeypatch.setattr(
        mono,
        "build_chromium_options",
        lambda hidden: (_Options(), "/nonexistent/spotiflac-test-profile"),
    )
    try:
        asyncio.run(session._ensure_browser())
        assert tab.visited == []
        assert session._on_page is False
    finally:
        asyncio.run(session._release_browser())


class _Options:
    def add_argument(self, argument):
        pass


def test_unreachable_primary_falls_back(session, monkeypatch) -> None:
    session._tab = FakeTab(unreachable={PRIMARY})
    solved_on = _solves_on(session, monkeypatch, FALLBACK)

    token = asyncio.run(session._solve_on_any_page(1.0))

    assert token == "jwt-from-" + FALLBACK
    assert session._tab.visited == [PRIMARY, FALLBACK]
    assert solved_on == [FALLBACK]
    assert session._page_url == FALLBACK


def test_primary_without_token_falls_back(session, monkeypatch) -> None:
    session._tab = FakeTab()
    solved_on = _solves_on(session, monkeypatch, FALLBACK)

    assert asyncio.run(session._solve_on_any_page(1.0)) == "jwt-from-" + FALLBACK
    assert solved_on == [PRIMARY, FALLBACK]


def test_every_page_down_reports_each(session, monkeypatch) -> None:
    session._tab = FakeTab(unreachable={PRIMARY, FALLBACK})
    _solves_on(session, monkeypatch)

    with pytest.raises(Exception, match="navigation failed") as info:
        asyncio.run(session._solve_on_any_page(1.0))
    assert PRIMARY in str(info.value) and FALLBACK in str(info.value)


def test_resolve_in_place_does_not_navigate_again(session, monkeypatch) -> None:
    session._tab = FakeTab()
    session._tab.url = session._page_url = FALLBACK
    session._on_page = True
    _solves_on(session, monkeypatch, FALLBACK)

    assert asyncio.run(session._solve_on_any_page(1.0)) == "jwt-from-" + FALLBACK
    assert session._tab.visited == []


def test_cached_token_still_loads_a_page_to_fetch_from(session, monkeypatch) -> None:
    session._tab = FakeTab(unreachable={PRIMARY})
    monkeypatch.setattr(mono, "monochrome_session_valid", lambda record: True)
    session._record.jwt = "cached-jwt"

    assert asyncio.run(session._ensure_token()) == "cached-jwt"
    assert session._tab.url == FALLBACK
    assert session._on_page is True

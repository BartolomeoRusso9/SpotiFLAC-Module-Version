"""Turnstile's iframe lives in a closed shadow root.

The solver's fallback used to look for it with
``document.querySelectorAll('iframe')``, which never sees inside a closed
shadow root, so on a challenge page that renders the widget after a countdown
— later than pydoll's own few-second bypass window — it found no widget and
clicked blind at the page's top-left corner, three times per attempt, until
the solve timed out. ``_locate_turnstile`` goes through CDP's shadow-root
enumeration instead; these fakes stand in for pydoll's nodes along that path.
"""

from __future__ import annotations

import asyncio
import inspect

from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement
from pydoll.exceptions import ElementNotFound

from SpotiFLAC.core import solver

WIDGET_HTML = '<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"></iframe>'
IFRAME_BOUNDS = {"x": 485.5, "y": 437.1875, "width": 300, "height": 65}


class FakeNode:
    def __init__(self, children=None, *, bounds=None, html=""):
        self._children = children or {}
        self._bounds = bounds
        self._html = html

    def _child(self, key):
        if key not in self._children:
            raise ElementNotFound()
        return self._children[key]

    @property
    async def inner_html(self):
        return self._html

    async def query(self, selector, timeout=0):
        return self._child(selector)

    async def find(self, tag_name=None, timeout=0):
        return self._child(tag_name)

    async def get_shadow_root(self, timeout=0):
        return self._child("#shadow-root")

    async def get_bounds_using_js(self):
        if self._bounds is None:
            raise KeyError("result")
        return self._bounds


class FakeTab:
    def __init__(self, shadow_roots):
        self._shadow_roots = shadow_roots

    async def find_shadow_roots(self, deep=False):
        return self._shadow_roots


def _widget(*, checkbox_rendered=True, bounds=IFRAME_BOUNDS):
    checkbox = FakeNode()
    inner_shadow = FakeNode(
        {solver._CF_CHECKBOX_SELECTOR: checkbox} if checkbox_rendered else {}
    )
    body = FakeNode({"#shadow-root": inner_shadow})
    iframe = FakeNode({"body": body}, bounds=bounds)
    root = FakeNode({solver._CF_IFRAME_SELECTOR: iframe}, html=WIDGET_HTML)
    return root, checkbox


def _locate(shadow_roots):
    return asyncio.run(solver._locate_turnstile(FakeTab(shadow_roots)))


def test_nothing_on_the_page_yet() -> None:
    assert _locate([]) == (None, None)


def test_shadow_roots_that_are_not_turnstile_are_skipped() -> None:
    unrelated = FakeNode(html="<slot></slot>")
    root, checkbox = _widget()
    assert _locate([unrelated, root])[0] is checkbox


def test_rendered_widget_yields_checkbox_and_page_rect() -> None:
    root, checkbox = _widget()
    found, rect = _locate([root])
    assert found is checkbox
    assert rect == {"x": 485.5, "y": 437.1875, "w": 300, "h": 65}


def test_widget_still_verifying_yields_rect_without_checkbox() -> None:
    # The window pydoll's bypass keeps missing: the widget is up, the
    # checkbox inside it is not rendered yet.
    root, _ = _widget(checkbox_rendered=False)
    assert _locate([root]) == (None, {"x": 485.5, "y": 437.1875, "w": 300, "h": 65})


def test_collapsed_widget_has_no_rect() -> None:
    # Before Turnstile renders, its container is zero-width — not a target.
    root, checkbox = _widget(bounds={"x": 635.5, "y": 440.0, "width": 0, "height": 65})
    assert _locate([root]) == (checkbox, None)


def test_turnstile_root_without_iframe_is_skipped() -> None:
    husk = FakeNode(html=WIDGET_HTML)
    assert _locate([husk]) == (None, None)


def test_installed_pydoll_has_the_apis_the_solver_uses() -> None:
    # What pyproject's pydoll-python>=2.19.0 floor is for: find_shadow_roots
    # and get_shadow_root arrived in 2.17.0, Tab.mouse (humanized moves) in
    # 2.19.0. On an older pydoll, locate_widget() would quietly lose the
    # checkbox and do_click() would raise AttributeError mid-solve.
    assert "deep" in inspect.signature(Tab.find_shadow_roots).parameters
    assert "timeout" in inspect.signature(WebElement.get_shadow_root).parameters
    assert isinstance(inspect.getattr_static(Tab, "mouse"), property)

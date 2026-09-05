"""The persistent Monochrome browser is closed when the run ends.

The Amazon provider's mono path (amz.geeked.wtf) keeps a real Chrome alive
between tracks on purpose — the JWT it gets is tied to that browser's TLS
session. What it must not do is keep it alive between *runs*: the session is
a module-level singleton rather than a provider, so `_close_providers()` did
not reach it and the only thing that ever shut it down was the process
exiting. The CLI exits after a run and got away with it; the TUI and the
desktop window stay up, and Chrome sat there after the download finished.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

from SpotiFLAC.downloader import DownloadWorker

_MONO = "SpotiFLAC.core.signed_session_mono"


def _worker() -> DownloadWorker:
    """A worker with no state — the hook touches none of it."""
    return DownloadWorker.__new__(DownloadWorker)


def _stub(on_close) -> types.ModuleType:
    module = types.ModuleType(_MONO)
    module.close_mono_browser_session = on_close
    return module


def test_the_mono_browser_is_closed_at_the_end_of_a_run(monkeypatch) -> None:
    closed: list[str] = []

    async def close() -> None:
        closed.append("closed")

    monkeypatch.setitem(sys.modules, _MONO, _stub(close))
    asyncio.run(_worker()._close_shared_browser_sessions())

    assert closed == ["closed"]


def test_a_run_that_never_touched_amazon_does_not_import_pydoll(monkeypatch) -> None:
    """The check reads `sys.modules` instead of importing.

    Importing `signed_session_mono` to ask whether a browser needs closing
    would pull in pydoll on every run, including the ones that never went
    near Amazon — and if the module was never imported, no mono browser was
    ever started and there is nothing to close.
    """
    monkeypatch.delitem(sys.modules, _MONO, raising=False)
    before = set(sys.modules)

    asyncio.run(_worker()._close_shared_browser_sessions())

    new_modules = set(sys.modules) - before
    assert not any(name.startswith("pydoll") for name in new_modules)
    assert _MONO not in sys.modules


def test_a_browser_that_will_not_close_does_not_hang_the_run(monkeypatch) -> None:
    """A download that already succeeded is not failed by a stuck teardown."""

    async def never_returns() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setitem(sys.modules, _MONO, _stub(never_returns))

    # Bound the wait down from the real 20s so this is not a 20s test. The
    # original has to be captured first: a patch that calls `asyncio.wait_for`
    # by name calls *itself*, and the RecursionError that follows is caught by
    # the same `suppress(Exception)` the hook uses — so the test would pass
    # while proving nothing about the timeout.
    real_wait_for = asyncio.wait_for

    async def briefly(awaitable, timeout):  # noqa: ARG001 — the point is to shrink it
        return await real_wait_for(awaitable, timeout=0.2)

    monkeypatch.setattr(asyncio, "wait_for", briefly)

    async def timed() -> float:
        started = time.monotonic()
        await _worker()._close_shared_browser_sessions()
        return time.monotonic() - started

    elapsed = asyncio.run(timed())
    assert 0.1 < elapsed < 3.0, "the teardown was not actually bounded by a timeout"


def test_a_close_that_raises_is_swallowed(monkeypatch) -> None:
    async def boom() -> None:
        msg = "chrome is already gone"
        raise RuntimeError(msg)

    monkeypatch.setitem(sys.modules, _MONO, _stub(boom))
    asyncio.run(_worker()._close_shared_browser_sessions())  # must not raise

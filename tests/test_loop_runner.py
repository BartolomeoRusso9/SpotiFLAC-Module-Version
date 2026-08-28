"""The shared background event loop that replaces per-call asyncio.run()."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import pytest

from SpotiFLAC.core import loop_runner


@pytest.fixture(autouse=True)
def _fresh_loop():
    loop_runner.shutdown()
    yield
    loop_runner.shutdown()


async def _echo(value, delay=0.0):
    if delay:
        await asyncio.sleep(delay)
    return value


def test_runs_a_coroutine_and_returns_its_result() -> None:
    assert loop_runner.run_coro(_echo(42)) == 42


def test_exceptions_propagate_unchanged() -> None:
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        loop_runner.run_coro(boom())


def test_every_call_shares_one_loop() -> None:
    """The whole point: a per-call asyncio.run() gave every call its own
    loop, so NetworkManager's client cache never hit and each call paid a
    fresh TLS handshake.
    """

    async def which_loop():
        return id(asyncio.get_running_loop())

    ids = {loop_runner.run_coro(which_loop()) for _ in range(5)}
    assert len(ids) == 1


def test_the_loop_survives_across_calls_from_different_threads() -> None:
    seen: list[int] = []

    async def which_loop():
        return id(asyncio.get_running_loop())

    def worker():
        seen.append(loop_runner.run_coro(which_loop()))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 4
    assert len(set(seen)) == 1


def test_loop_bound_state_persists_between_calls() -> None:
    """An asyncio primitive created under one call must still be usable in
    the next — this is what per-call asyncio.run() made impossible, and why
    the module-level rate limiters in core/http.py misbehaved.
    """
    holder: dict[str, asyncio.Event] = {}

    async def make():
        holder["event"] = asyncio.Event()

    async def use():
        holder["event"].set()
        return holder["event"].is_set()

    loop_runner.run_coro(make())
    assert loop_runner.run_coro(use()) is True


def test_calling_from_inside_the_loop_raises_instead_of_deadlocking() -> None:
    async def reenter():
        return loop_runner.run_coro(_echo(1))

    with pytest.raises(RuntimeError, match="deadlock"):
        loop_runner.run_coro(reenter())


def test_timeout_cancels_and_raises() -> None:
    # Only an alias of the builtin from 3.11 on; on 3.10 (which this project
    # still supports) it is a separate class that does not inherit from it,
    # so naming the builtin here would miss what run_coro actually re-raises.
    with pytest.raises(concurrent.futures.TimeoutError):
        loop_runner.run_coro(_echo("slow", delay=5), timeout=0.1)


def test_submit_does_not_block() -> None:
    # Prime the loop first: otherwise the measurement includes starting the
    # background thread, which is exactly the sort of one-off cost that
    # makes a timing assertion flake on a loaded machine.
    loop_runner.run_coro(_echo("warm"))
    started = time.monotonic()
    future = loop_runner.submit(_echo("later", delay=0.3))
    assert time.monotonic() - started < 0.2, "submit() blocked"
    assert future.result(timeout=5) == "later"


def test_shutdown_is_idempotent_and_restarts_on_next_use() -> None:
    assert loop_runner.run_coro(_echo(1)) == 1
    loop_runner.shutdown()
    loop_runner.shutdown()  # second call must be a no-op, not an error
    assert loop_runner.run_coro(_echo(2)) == 2


def test_shutdown_without_ever_starting_is_safe() -> None:
    loop_runner.shutdown()


def test_the_loop_thread_is_a_daemon() -> None:
    """It must never hold the interpreter open: a CLI download that finishes
    should exit, not hang waiting for an idle loop.
    """
    loop_runner.run_coro(_echo(1))
    thread = next(t for t in threading.enumerate() if t.name == "spotiflac-loop")
    assert thread.daemon


# ── run_sync: the replacement for the four copy-pasted _run_async_sync shims ──


def test_run_sync_works_with_no_running_loop() -> None:
    assert loop_runner.run_sync(_echo("ok")) == "ok"


def test_run_sync_uses_the_shared_loop_rather_than_making_one() -> None:
    """The old shim called asyncio.run() in every branch, so each call lost
    the connection pool and every other piece of loop-bound state.
    """

    async def which_loop():
        return id(asyncio.get_running_loop())

    shared = loop_runner.run_coro(which_loop())
    assert loop_runner.run_sync(which_loop()) == shared


def test_run_sync_from_another_loop_still_reaches_the_shared_one() -> None:
    async def outer():
        # A different loop entirely: blocking this thread on the shared loop
        # is fine, they are different threads.
        return await asyncio.to_thread(loop_runner.run_sync, _echo("via-thread"))

    assert asyncio.run(outer()) == "via-thread"


def test_run_sync_from_inside_the_shared_loop_does_not_deadlock() -> None:
    """The one case that genuinely cannot use the shared loop. run_coro()
    raises here; run_sync() has to keep working, because it backs sync APIs
    that async code legitimately calls into.
    """

    async def reenter():
        return loop_runner.run_sync(_echo("nested"))

    assert loop_runner.run_coro(reenter()) == "nested"

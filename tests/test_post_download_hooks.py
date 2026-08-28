"""Post-download hooks — your own Python called per track, with objects.

The typed counterpart to `--post-action=command`: no shell, no escaping,
and reachable per-track rather than once per batch.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.hooks import HookLoadError, load_hook, load_hooks, run_hooks
from SpotiFLAC.core.models import DownloadResult

# ── module-level targets for load_hook to resolve ──────────────────────────

calls: list[tuple] = []


def record(result, metadata) -> None:
    calls.append(("sync", result, metadata))


async def record_async(result, metadata) -> None:
    calls.append(("async", result, metadata))


def explode(result, metadata) -> None:
    raise RuntimeError("hook is broken")


not_callable = "I am a string"


class Holder:
    @staticmethod
    def nested(result, metadata) -> None:
        calls.append(("nested", result, metadata))


@pytest.fixture(autouse=True)
def _clear():
    calls.clear()
    yield
    calls.clear()


@pytest.fixture
def result():
    return DownloadResult(success=True, provider="test", file_path="/tmp/a.flac")


# ── loading ────────────────────────────────────────────────────────────────


def test_loads_a_module_level_function() -> None:
    assert load_hook(f"{__name__}:record") is record


def test_loads_a_dotted_attribute() -> None:
    assert load_hook(f"{__name__}:Holder.nested") is Holder.nested


@pytest.mark.parametrize(
    "spec",
    [
        "no_colon_here",
        ":missing_module",
        f"{__name__}:",
    ],
)
def test_malformed_specs_are_rejected(spec) -> None:
    with pytest.raises(HookLoadError):
        load_hook(spec)


def test_a_missing_module_names_itself_in_the_error() -> None:
    with pytest.raises(HookLoadError, match="nope_does_not_exist"):
        load_hook("nope_does_not_exist:thing")


def test_a_missing_attribute_names_itself_in_the_error() -> None:
    with pytest.raises(HookLoadError, match="no_such_function"):
        load_hook(f"{__name__}:no_such_function")


def test_a_non_callable_target_is_rejected() -> None:
    with pytest.raises(HookLoadError, match="not callable"):
        load_hook(f"{__name__}:not_callable")


def test_hooks_are_resolved_eagerly() -> None:
    """A typo must fail when the run is configured, not silently do nothing
    after a two-hour discography download.
    """
    with pytest.raises(HookLoadError):
        load_hooks([f"{__name__}:record", f"{__name__}:typo"])


def test_no_hooks_configured_is_not_an_error() -> None:
    assert load_hooks(None) == []
    assert load_hooks([]) == []


# ── running ────────────────────────────────────────────────────────────────


def test_a_sync_hook_receives_the_result_and_metadata(result) -> None:
    asyncio.run(run_hooks([record], result, "meta"))
    assert calls == [("sync", result, "meta")]


def test_an_async_hook_is_awaited(result) -> None:
    asyncio.run(run_hooks([record_async], result, "meta"))
    assert calls == [("async", result, "meta")]


def test_every_hook_runs_in_order(result) -> None:
    asyncio.run(run_hooks([record, record_async], result, "meta"))
    assert [c[0] for c in calls] == ["sync", "async"]


def test_a_failing_hook_does_not_stop_the_others(result, caplog) -> None:
    """A broken notifier must not turn a completed download into a failed
    one, nor prevent the next hook from running.
    """
    asyncio.run(run_hooks([explode, record], result, "meta"))
    assert [c[0] for c in calls] == ["sync"]
    assert "hook is broken" in caplog.text


def test_hooks_run_for_failed_downloads_too() -> None:
    """Reporting a failure is as reasonable a use for a hook as reporting a
    success — the hook decides, by checking result.success.
    """
    failed = DownloadResult(success=False, provider="test", error="nope")
    asyncio.run(run_hooks([record], failed, "meta"))
    assert calls[0][1].success is False


def test_a_sync_hook_does_not_block_the_event_loop(result) -> None:
    """Sync hooks go to a worker thread: hooks are exactly where people put
    slow work (a library scan, a beets import), and that must not stall the
    loop driving the other concurrent downloads.
    """
    import threading

    seen: list[int] = []

    def which_thread(res, meta) -> None:
        seen.append(threading.get_ident())

    async def main():
        await run_hooks([which_thread], result, "meta")
        return threading.get_ident()

    loop_thread = asyncio.run(main())
    assert seen and seen[0] != loop_thread

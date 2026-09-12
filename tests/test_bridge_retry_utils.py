"""The three `utils` functions the bridge never provided.

Extensions guard on them with `typeof utils.x === "function"`, which is what
made their absence so expensive: nothing failed, the guarded behaviour just
did not happen. In tidal-web's download retry loop all three are missing at
once (`index.js`):

    waitBeforeRetry()          ends in `return true` — no backoff at all, and
                               the exponential delay and any Retry-After the
                               gateway asked for are computed and discarded
    isDownloadCancelled()      never consulted, so a cancelled run keeps
                               retrying
    getResolutionRemainingMs() never consulted, so nothing caps the loop

With `maxDownloadAttempts: 5` and a gateway `/dl` that goes quiet for its
full read timeout, that is five back-to-back 30s stalls per quality tier,
with no delay between them and no way to cut it short.

These drive the real _bridge.js under the real Node, because what is worth
pinning is what a plausible implementation gets wrong: a sleep that does not
block, a budget read off the wrong clock, and a cancellation probe that
reports "cancelled" when it simply could not be answered.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from SpotiFLAC.extensions.runtime import JSRuntime

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node to exercise the real bridge"
)

PROBE = """
registerExtension({
  initialize: function () {},
  available: function () {
    return {
      sleep: typeof utils.sleep === "function",
      budget: typeof utils.getResolutionRemainingMs === "function",
      cancelled: typeof utils.isDownloadCancelled === "function"
    };
  },
  sleepFor: function (ms) {
    var started = Date.now();
    var returned = utils.sleep(ms);
    return { elapsed: Date.now() - started, returned: returned };
  },
  budget: function () {
    return utils.getResolutionRemainingMs();
  },
  cancelled: function () {
    return utils.isDownloadCancelled();
  }
});
"""


@pytest.fixture
def runtime(tmp_path: Path):
    """A started runtime whose cancellation answer the test controls."""
    ext = tmp_path / "index.js"
    ext.write_text(PROBE)
    state = {"cancelled": False}
    rt = JSRuntime(ext_path=ext, cancelled_probe=lambda: state["cancelled"])
    rt.start()
    try:
        yield rt, state
    finally:
        rt.stop()


def test_the_extension_api_offers_all_three(runtime) -> None:
    rt, _ = runtime
    assert rt.call("available") == {"sleep": True, "budget": True, "cancelled": True}


def test_sleep_actually_blocks_the_extension_thread(runtime) -> None:
    """A sleep that returns immediately is worse than none.

    `waitBeforeRetry()` treats a truthy return as "the wait happened", so a
    no-op that reports success turns every backoff into a busy retry.
    """
    rt, _ = runtime
    started = time.monotonic()
    result = rt.call("sleepFor", 400, timeout=25.0)
    host_elapsed = time.monotonic() - started

    assert result["returned"] is True
    assert result["elapsed"] >= 380
    # Blocked inside the call, not after it returned.
    assert host_elapsed >= 0.38


def test_the_budget_comes_from_the_host_s_own_deadline(runtime) -> None:
    """`timeout` on the call *is* the budget — see JSRuntime.call.

    Measured from the moment the worker receives the call rather than from a
    stamp the host puts in the message: the two run on clocks that need not
    agree, and the gap between them would read as budget already spent.
    """
    rt, _ = runtime
    remaining = rt.call("budget", timeout=20.0)
    assert 18_000 <= remaining <= 20_000

    # A different deadline per call, not a value fixed at startup.
    assert rt.call("budget", timeout=5.0) <= 5_000


def test_cancellation_is_read_from_the_host_while_the_call_runs(runtime) -> None:
    rt, state = runtime
    assert rt.call("cancelled") is False

    state["cancelled"] = True
    # Outlast the worker's short cache, which exists so an extension polling
    # in a tight loop does not turn into one round trip per iteration.
    time.sleep(0.3)
    assert rt.call("cancelled") is True


def test_an_unanswerable_probe_reads_as_running(tmp_path: Path) -> None:
    """Saying "cancelled" on a failed probe would abort a healthy download."""
    ext = tmp_path / "index.js"
    ext.write_text(PROBE)

    def _raises() -> bool:
        msg = "no run is attached to this provider"
        raise RuntimeError(msg)

    rt = JSRuntime(ext_path=ext, cancelled_probe=_raises)
    rt.start()
    try:
        assert rt.call("cancelled") is False
    finally:
        rt.stop()


def test_no_probe_configured_reads_as_running(tmp_path: Path) -> None:
    ext = tmp_path / "index.js"
    ext.write_text(PROBE)
    rt = JSRuntime(ext_path=ext)
    rt.start()
    try:
        assert rt.call("cancelled") is False
    finally:
        rt.stop()

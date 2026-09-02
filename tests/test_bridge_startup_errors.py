"""What the host says when the extension bridge will not start.

Every startup failure used to arrive as the same sentence — "Extension did
not respond within 30.0s. Verify that the JS file is valid." — after a full
thirty-second wait, whatever had actually happened. Node normally dies in
well under a second and explains itself on stderr; that output went only to
a debug log, so the one message that could have identified the problem was
the one nobody saw. A user on Windows reporting this had no way to tell a
rejected NODE_OPTIONS flag from a broken extension from a missing runtime.

These tests pin down that a dead Node is noticed immediately rather than
waited out, and that its own words reach the exception.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from SpotiFLAC.extensions.runtime import ExtensionRuntimeError, JSRuntime

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node to exercise the real bridge"
)


def test_a_broken_extension_reports_what_node_said(tmp_path: Path) -> None:
    ext = tmp_path / "broken.js"
    ext.write_text("this is not ( valid javascript")

    rt = JSRuntime(ext, startup_timeout=30.0)
    started = time.monotonic()
    with pytest.raises(ExtensionRuntimeError) as caught:
        rt.start()
    elapsed = time.monotonic() - started

    message = str(caught.value)
    # The parse error itself, not a guess about it. The bridge's worker
    # handler reports e.message, so it is the parser's own wording.
    assert "Unexpected identifier" in message
    assert "broken.js" in message
    assert "Verify that the JS file is valid" not in message
    # And it must not have sat out the whole timeout to say so.
    assert elapsed < 15


def test_an_extension_that_never_registers_is_reported_with_its_error(
    tmp_path: Path,
) -> None:
    ext = tmp_path / "silent.js"
    # Loads and runs, but never calls registerExtension(): the bridge throws.
    ext.write_text("var x = 1;")

    rt = JSRuntime(ext, startup_timeout=30.0)
    with pytest.raises(ExtensionRuntimeError) as caught:
        rt.start()

    assert "registerExtension" in str(caught.value)


def test_a_node_that_refuses_its_options_is_not_blamed_on_the_js(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rejected NODE_OPTIONS flag is the failure mode that looks least like
    # what the old message claimed: the extension is fine and never runs.
    monkeypatch.setenv("NODE_OPTIONS", "--no-such-flag-exists")
    monkeypatch.setenv("SPOTIFLAC_EXT_NO_SANDBOX", "1")  # keep NODE_OPTIONS
    ext = tmp_path / "fine.js"
    ext.write_text("registerExtension({ initialize: function () {} });")

    rt = JSRuntime(ext, startup_timeout=30.0)
    started = time.monotonic()
    with pytest.raises(ExtensionRuntimeError) as caught:
        rt.start()
    elapsed = time.monotonic() - started

    message = str(caught.value)
    assert "bad option" in message or "not allowed" in message, message
    assert "Verify that the JS file is valid" not in message
    assert elapsed < 15


def test_a_healthy_extension_still_starts(tmp_path: Path) -> None:
    ext = tmp_path / "ok.js"
    ext.write_text(
        "registerExtension({ initialize: function () {},"
        " ping: function () { return 'pong'; } });"
    )

    rt = JSRuntime(ext, startup_timeout=30.0)
    rt.start()
    try:
        assert rt.call("ping") == "pong"
    finally:
        rt.stop()

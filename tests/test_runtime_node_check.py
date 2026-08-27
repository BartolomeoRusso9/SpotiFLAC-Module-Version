"""Tests for the node-check branch JSRuntime.start() added in
extensions/runtime.py — that Node missing now goes through
core.node_check.ensure_node_installed() before giving up, instead of
raising immediately. Only that branch is exercised here (with a fake
extension path just past it): the rest of start() spawns a real Node
subprocess and belongs to an integration test, not this unit test.
"""

from __future__ import annotations

import pytest

from SpotiFLAC.core import node_check
from SpotiFLAC.extensions.runtime import ExtensionRuntimeError, JSRuntime


def test_start_attempts_auto_install_when_node_missing(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_ensure_node_installed(node_executable, **kwargs):
        calls.append(node_executable)
        return {"available": False, "version": "", "error": "induced failure"}

    monkeypatch.setattr("SpotiFLAC.extensions.runtime.shutil.which", lambda _name: None)
    monkeypatch.setattr(node_check, "ensure_node_installed", fake_ensure_node_installed)

    rt = JSRuntime(ext_path=tmp_path / "does-not-matter.js")

    with pytest.raises(ExtensionRuntimeError) as exc_info:
        rt.start()

    assert calls == ["node"]
    assert "induced failure" in str(exc_info.value)
    assert "Node.js not found" in str(exc_info.value)


def test_start_proceeds_past_node_check_when_auto_install_succeeds(
    monkeypatch, tmp_path
) -> None:
    """Once the (mocked) install reports success, start() must move on to
    its next real check instead of raising for "Node.js not found" — here
    that's the extension file itself, which we point at something that
    doesn't exist so start() still stops (safely, before ever spawning a
    real Node subprocess) but for a *different*, later reason.
    """
    monkeypatch.setattr("SpotiFLAC.extensions.runtime.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        node_check,
        "ensure_node_installed",
        lambda node_executable, **kwargs: {
            "available": True,
            "version": "v20.0.0",
            "error": "",
        },
    )

    rt = JSRuntime(ext_path=tmp_path / "does-not-exist.js")

    with pytest.raises(ExtensionRuntimeError) as exc_info:
        rt.start()

    assert "Extension not found" in str(exc_info.value)
    assert "Node.js" not in str(exc_info.value)

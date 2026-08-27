"""Tests for the auto-install branch transcode.ensure_ffmpeg_available()
added in core/transcode.py — a missing ffmpeg now goes through
core.ffmpeg_check.ensure_ffmpeg_installed() before giving up, instead of
raising straight off a plain check_ffmpeg(). Mirrors
tests/test_runtime_node_check.py's pattern for the equivalent Node.js
branch in extensions/runtime.py's JSRuntime.start().
"""

from __future__ import annotations

import pytest

import SpotiFLAC.core.ffmpeg_check as ffmpeg_check
from SpotiFLAC.core import transcode
from SpotiFLAC.core.errors import SpotiflacError


def test_ensure_ffmpeg_available_attempts_auto_install_when_missing(
    monkeypatch,
) -> None:
    calls = []

    def fake_ensure_ffmpeg_installed(**kwargs):
        calls.append(kwargs)
        return {"available": False, "version": "", "error": "induced failure"}

    monkeypatch.setattr(
        ffmpeg_check, "ensure_ffmpeg_installed", fake_ensure_ffmpeg_installed
    )

    with pytest.raises(SpotiflacError) as exc_info:
        transcode.ensure_ffmpeg_available("mp3")

    assert calls == [{}]
    assert "induced failure" in str(exc_info.value)
    assert "MP3" in str(exc_info.value)


def test_ensure_ffmpeg_available_passes_when_auto_install_succeeds(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ffmpeg_check,
        "ensure_ffmpeg_installed",
        lambda **kwargs: {
            "available": True,
            "version": "ffmpeg version 7.0",
            "error": "",
        },
    )

    transcode.ensure_ffmpeg_available("mp3")  # must not raise

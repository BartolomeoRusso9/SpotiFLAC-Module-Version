"""Tests for api_mixins/dedup.py — thin-wrapper behavior; the actual
fingerprint/grouping logic is covered by tests/test_audio_fingerprint.py.
"""

from __future__ import annotations

from SpotiFLAC.api_mixins.dedup import DedupMixin
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.webapp import ALLOWED_METHODS


def test_dedup_methods_are_web_allowed_and_resolve_to_the_mixin() -> None:
    for name in ("get_dedup_status", "scan_for_duplicates"):
        assert name in ALLOWED_METHODS
        assert name in DedupMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith("DedupMixin.")


def test_get_dedup_status_shape() -> None:
    api = SpotiFLAC_API()
    status = api.get_dedup_status()
    assert "available" in status
    assert isinstance(status["available"], bool)
    if not status["available"]:
        assert status["install_hint"]


def test_scan_for_duplicates_rejects_missing_path() -> None:
    api = SpotiFLAC_API()
    assert api.scan_for_duplicates("") == {
        "status": "error",
        "error": "No path given",
    }


def test_scan_for_duplicates_rejects_nonexistent_path() -> None:
    api = SpotiFLAC_API()
    result = api.scan_for_duplicates("/this/does/not/exist/anywhere")
    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_scan_for_duplicates_rejects_path_outside_approved_roots() -> None:
    import os
    from pathlib import Path

    if not os.path.isdir("/etc"):
        return  # no /etc on this platform (e.g. plain Windows CI) — skip
    home = Path.home().resolve()
    try:
        Path("/etc").resolve().relative_to(home)
        return  # pathological sandbox where /etc is under $HOME — skip
    except ValueError:
        pass

    api = SpotiFLAC_API()
    result = api.scan_for_duplicates("/etc")
    assert result["status"] == "error"
    assert "Access denied" in result["error"]


# ── Library-wide duplicates (core/library_dedup.py) ───────────────────────

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

ffmpeg_required = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs ffmpeg to build audio fixtures"
)


def _make_audio(path: Path, *, codec: str, **tags: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: list[str] = []
    for key, value in tags.items():
        metadata += ["-metadata", f"{key}={value}"]
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2:sample_rate=44100",
            "-c:a",
            codec,
            *metadata,
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture()
def api_on(tmp_path, monkeypatch):
    """An API instance whose approved root is a throwaway folder, with its
    push events captured instead of sent."""
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "cache"))
    api = SpotiFLAC_API()
    api.download_dir = str(tmp_path)
    api.pushed = []
    api._push = lambda name, *args: api.pushed.append((name, args[0] if args else None))
    return api


def test_library_dedup_methods_are_web_allowed_and_resolve_to_the_mixin() -> None:
    for name in (
        "scan_library_duplicates",
        "resolve_library_duplicates",
        "restore_library_duplicates",
    ):
        assert name in ALLOWED_METHODS
        assert name in DedupMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith("DedupMixin.")


def test_scan_library_duplicates_validates_its_path() -> None:
    api = SpotiFLAC_API()
    assert api.scan_library_duplicates("")["error"] == "No path given"
    assert "does not exist" in api.scan_library_duplicates("/nope/nowhere")["error"]


def test_scan_library_duplicates_rejects_an_unknown_match_mode(
    api_on, tmp_path
) -> None:
    result = api_on.scan_library_duplicates(str(tmp_path), match="vibes")
    assert result["status"] == "error"
    assert "match mode" in result["error"]


def test_resolving_without_a_scan_says_so() -> None:
    api = SpotiFLAC_API()
    result = api.resolve_library_duplicates()
    assert result["status"] == "error"
    assert "run a library duplicate scan first" in result["error"]


@ffmpeg_required
def test_scan_pushes_a_report_and_resolve_acts_on_the_selection(api_on, tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")

    # The thread body directly: what it pushes is the contract, and running
    # it inline keeps the test from racing a daemon thread.
    api_on._scan_library_duplicates_thread(
        str(tmp_path), True, "both", 4.0, False, 0.95, True
    )

    events = dict(api_on.pushed)
    assert "app_library_dedup_results" in events
    report = events["app_library_dedup_results"]
    assert report["library"]["files"] == 2
    assert report["groups"] == 1
    assert report["shown_groups"] == 1
    assert report["database"].endswith("library-index.db")
    assert Path(report["database"]).exists()

    duplicate = report["duplicate_groups"][0]["duplicates"][0]["path"]
    result = api_on.resolve_library_duplicates(paths=[duplicate])

    assert result["status"] == "ok"
    assert result["resolved"] == 1
    assert not Path(duplicate).exists()
    assert (tmp_path / "a.flac").exists()

    # The report described a library that no longer exists.
    assert api_on.resolve_library_duplicates()["status"] == "error"

    restored = api_on.restore_library_duplicates(result["manifest"])
    assert restored["status"] == "ok"
    assert Path(duplicate).exists()


@ffmpeg_required
def test_resolve_refuses_a_path_the_scan_never_reported(api_on, tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")
    api_on._scan_library_duplicates_thread(
        str(tmp_path), True, "both", 4.0, False, 0.95, False
    )

    result = api_on.resolve_library_duplicates(paths=["/etc/passwd"])
    assert result["status"] == "error"
    assert "None of those files" in result["error"]
    assert Path(tmp_path / "b.mp3").exists()


@ffmpeg_required
def test_the_kept_copy_is_never_removed_even_if_asked(api_on, tmp_path):
    _make_audio(tmp_path / "a.flac", codec="flac", title="Song", artist="A")
    _make_audio(tmp_path / "b.mp3", codec="libmp3lame", title="Song", artist="A")
    api_on._scan_library_duplicates_thread(
        str(tmp_path), True, "both", 4.0, False, 0.95, False
    )
    report = dict(api_on.pushed)["app_library_dedup_results"]
    keeper = report["duplicate_groups"][0]["keep"]["path"]

    result = api_on.resolve_library_duplicates(paths=[keeper])
    assert result["status"] == "error"
    assert Path(keeper).exists()

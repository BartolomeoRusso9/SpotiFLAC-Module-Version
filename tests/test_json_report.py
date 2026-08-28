"""`--json` — the run as data instead of scraped console text."""

from __future__ import annotations

import json

import pytest

from SpotiFLAC.core.hooks import load_hooks, run_hooks
from SpotiFLAC.core.models import DownloadResult
from SpotiFLAC.core.report import RunReport


class _Meta:
    def __init__(
        self, id="1", title="Title", artists="Artist", album="Album", isrc="X"
    ):
        self.id, self.title, self.artists = id, title, artists
        self.album, self.isrc = album, isrc


def _ok(path="/music/a.flac"):
    return DownloadResult(success=True, provider="tidal", file_path=path, format="flac")


def _failed(error="no provider"):
    return DownloadResult(success=False, provider="none", error=error)


def _skipped():
    return DownloadResult(
        success=True, provider="tidal", file_path="/music/a.flac", skipped=True
    )


def test_a_successful_track_is_recorded_with_its_details() -> None:
    report = RunReport()
    report(_ok(), _Meta(title="Song", artists="Band"))

    (track,) = report.to_dict()["tracks"]
    assert track["status"] == "downloaded"
    assert track["title"] == "Song"
    assert track["artists"] == "Band"
    assert track["provider"] == "tidal"
    assert track["format"] == "flac"
    assert track["file_path"] == "/music/a.flac"
    assert track["error"] is None


def test_statuses_distinguish_downloaded_skipped_and_failed() -> None:
    report = RunReport()
    report(_ok(), _Meta(id="1"))
    report(_skipped(), _Meta(id="2"))
    report(_failed("boom"), _Meta(id="3"))

    doc = report.to_dict()
    assert doc["summary"] == {
        "total": 3,
        "downloaded": 1,
        "skipped": 1,
        "failed": 1,
    }
    assert [t["status"] for t in doc["tracks"]] == [
        "downloaded",
        "skipped",
        "failed",
    ]
    assert doc["tracks"][2]["error"] == "boom"


def test_a_skipped_track_is_not_counted_as_downloaded() -> None:
    """`skipped` rides on a successful DownloadResult, so checking
    `success` alone would over-report what was actually fetched.
    """
    report = RunReport()
    report(_skipped(), _Meta())
    assert report.to_dict()["summary"]["downloaded"] == 0


def test_the_document_is_valid_json_and_declares_its_schema() -> None:
    report = RunReport()
    report(_ok(), _Meta())
    doc = json.loads(report.to_json())
    assert doc["schema_version"] == RunReport.SCHEMA_VERSION
    assert doc["finished_at"] >= doc["started_at"]


def test_an_empty_run_still_produces_a_valid_document() -> None:
    """A script must never have to tell "no JSON" apart from "no tracks"."""
    doc = json.loads(RunReport().to_json())
    assert doc["summary"]["total"] == 0
    assert doc["tracks"] == []


def test_failed_exposes_only_the_failures() -> None:
    report = RunReport()
    report(_ok(), _Meta(id="1"))
    report(_failed(), _Meta(id="2", title="Broken"))
    assert [r.title for r in report.failed] == ["Broken"]


def test_the_report_plugs_into_the_hook_pipeline_unchanged() -> None:
    """--json is implemented as a hook rather than as a parallel path, so a
    user's own --post-hook and the report observe identical events.
    """
    import asyncio

    report = RunReport()
    seen: list = []

    hooks = load_hooks([report, lambda result, meta: seen.append(meta.id)])
    asyncio.run(run_hooks(hooks, _ok(), _Meta(id="42")))

    assert seen == ["42"]
    assert report.to_dict()["tracks"][0]["id"] == "42"


def test_records_survive_concurrent_hook_threads() -> None:
    """Sync hooks are dispatched to worker threads, so several tracks can
    call record() at once.
    """
    import threading

    report = RunReport()

    def add(i):
        report(_ok(), _Meta(id=str(i)))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert report.to_dict()["summary"]["total"] == 50


@pytest.mark.parametrize("missing", ["title", "artists", "album", "isrc"])
def test_missing_metadata_fields_do_not_break_the_report(missing) -> None:
    meta = _Meta()
    setattr(meta, missing, None)
    report = RunReport()
    report(_ok(), meta)
    assert report.to_dict()["tracks"][0][missing] == ""

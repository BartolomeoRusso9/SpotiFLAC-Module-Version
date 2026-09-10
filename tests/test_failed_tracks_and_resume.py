"""Failed tracks outlive the run, and a --web download outlives a restart.

Before, a download started from the web UI lived in a bare thread: a
container update halfway through a 1,800-track playlist lost the batch, and
nothing remembered which tracks had failed — the only way to find them again
was to run the whole playlist. These tests pin the two halves of the fix: a
download job carries its own tracks (not positions in a tracklist that is
gone after a restart), and failures stay listed until they download.
"""

from __future__ import annotations

import time

import pytest

import SpotiFLAC as spotiflac_pkg
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.core import db, failed_tracks
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

PLAYLIST = "https://open.spotify.com/playlist/p"


def _track(n: int) -> TrackMetadata:
    return TrackMetadata(
        id=f"sp{n}",
        title=f"Song {n}",
        artists="Artist",
        album="Album",
        album_artist="Artist",
        isrc=f"ITAAA000000{n}",
        external_url=f"https://open.spotify.com/track/sp{n}",
    )


class _RecordingQueue:
    """Stands in for the persisted JobQueue: keeps what was submitted."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    def submit(self, owner: str, payload: dict) -> None:
        self.jobs.append((owner, payload))


@pytest.fixture()
def calls(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", lambda **kw: seen.append(kw))
    return seen


def _api(tmp_path, tracks: int = 3) -> SpotiFLAC_API:
    api = SpotiFLAC_API()
    api.download_dir = str(tmp_path)
    api.current_tracks = [_track(n) for n in range(tracks)]
    api.current_url = PLAYLIST
    return api


# ── core/failed_tracks.py ─────────────────────────────────────────────────


def test_a_failure_is_listed_and_a_later_success_clears_it(tmp_path):
    hook = failed_tracks.hook(source_url=PLAYLIST)
    hook(DownloadResult.fail("tidal", "no stream"), _track(1))
    hook(DownloadResult.fail("qobuz", "not found"), _track(1))

    [item] = failed_tracks.list_for()
    assert item["title"] == "Song 1"
    assert item["attempts"] == 2
    assert item["error"] == "not found"

    target = tmp_path / "song.m4a"
    target.write_bytes(b"x")
    hook(DownloadResult.ok("tidal", str(target)), _track(1))
    assert failed_tracks.list_for() == []


def test_a_skip_means_the_track_is_there(tmp_path):
    hook = failed_tracks.hook()
    hook(DownloadResult.fail("tidal", "boom"), _track(2))
    hook(DownloadResult.skipped_result("tidal", str(tmp_path / "x.m4a")), _track(2))
    assert failed_tracks.list_for() == []


def test_each_account_has_its_own_list():
    failed_tracks.hook(owner="alice")(DownloadResult.fail("t", "e"), _track(3))
    assert len(failed_tracks.list_for("alice")) == 1
    assert failed_tracks.list_for("bob") == []


def test_retry_groups_keep_the_source_and_clear_takes_keys():
    failed_tracks.hook(source_url=PLAYLIST)(DownloadResult.fail("t", "e"), _track(4))
    tidal = "https://tidal.com/browse/playlist/b"
    failed_tracks.hook(source_url=tidal)(DownloadResult.fail("t", "e"), _track(5))

    groups = dict(failed_tracks.retry_groups())
    assert set(groups) == {PLAYLIST, tidal}
    assert groups[PLAYLIST][0]["id"] == "sp4"

    assert failed_tracks.clear(keys=[failed_tracks.track_key(_track(4))]) == 1
    assert [i["key"] for i in failed_tracks.list_for()] == [
        failed_tracks.track_key(_track(5))
    ]
    assert failed_tracks.clear() == 1


# ── Persisted download jobs ───────────────────────────────────────────────


def test_a_web_download_is_written_down_as_tracks_not_positions(tmp_path, calls):
    api = _api(tmp_path)
    api._download_queue = _RecordingQueue()

    api.download_tracks([0, 2], {"services": ["tidal"]})

    [(_, payload)] = api._download_queue.jobs
    assert calls == [], "nothing runs until the queue picks the job up"
    assert payload["indices"] == [0, 2]
    assert [t["id"] for t in payload["tracks"]] == ["sp0", "sp2"]
    assert payload["whole"] is False
    assert payload["source_url"] == PLAYLIST


def test_a_job_resumes_after_a_restart_without_the_tracklist(tmp_path, calls):
    before = _api(tmp_path)
    before._download_queue = _RecordingQueue()
    before.download_tracks([0, 2], {"services": ["tidal"]})
    # Exactly what the jobs table hands back to the next process.
    payload = db.loads(db.dumps(before._download_queue.jobs[0][1]))

    after = SpotiFLAC_API()  # empty tracklist, new session
    after.download_dir = str(tmp_path)
    pushed: list[tuple[str, tuple]] = []
    after._push = lambda name, *args: pushed.append((name, args))
    after.run_download_job(payload)

    [call] = calls
    assert call["url"] == [
        "https://open.spotify.com/track/sp0",
        "https://open.spotify.com/track/sp2",
    ]
    assert set(call["prefetched_tracks"]) == set(call["url"])
    names = [name for name, _ in pushed]
    assert "app_background_download_finished" in names
    assert (
        "app_download_finished" not in names
    ), "a restored job must not close a batch the new page dispatched"


def test_a_whole_playlist_resumes_as_the_playlist(tmp_path, calls):
    api = _api(tmp_path)
    api._download_queue = _RecordingQueue()
    api.download_tracks([0, 1, 2], {"services": ["tidal"]})
    payload = api._download_queue.jobs[0][1]
    assert payload["whole"] is True

    fresh = SpotiFLAC_API()
    fresh.download_dir = str(tmp_path)
    fresh.run_download_job(payload)
    assert calls[-1]["url"] == PLAYLIST


def test_this_sessions_job_still_closes_its_own_batch(tmp_path, calls):
    api = _api(tmp_path)
    api._download_queue = _RecordingQueue()
    api.download_tracks([1], {"services": ["tidal"]})
    pushed: list[tuple[str, tuple]] = []
    api._push = lambda name, *args: pushed.append((name, args))

    api.run_download_job(api._download_queue.jobs[0][1])

    assert ("app_download_finished", (True, [1])) in pushed


def test_the_desktop_window_still_downloads_straight_away(tmp_path, calls):
    api = _api(tmp_path)  # no queue: what the desktop build gets
    api.download_tracks([0], {"services": ["tidal"]})
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls, "without a queue the batch runs as it always has"


def test_a_batchs_failures_are_listed_with_their_source(tmp_path, monkeypatch):
    def _fake_download(**kw):
        for hook in kw["post_download_hooks"]:
            hook(DownloadResult.fail("tidal", "no stream"), _track(1))

    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", _fake_download)
    _api(tmp_path)._download_task([1], {"services": ["tidal"]})

    [item] = failed_tracks.list_for()
    assert item["title"] == "Song 1"
    [(source, _)] = failed_tracks.retry_groups()
    assert source == PLAYLIST


def test_retry_submits_the_missing_tracks_as_a_background_job():
    failed_tracks.hook(source_url=PLAYLIST)(DownloadResult.fail("t", "e"), _track(7))
    api = SpotiFLAC_API()
    api._download_queue = _RecordingQueue()

    assert api.retry_failed_tracks({"services": ["tidal"]}) == {"queued": 1}

    [(_, payload)] = api._download_queue.jobs
    assert payload["whole"] is False
    assert payload["tracks"][0]["id"] == "sp7"
    assert "session" not in payload, "no open page dispatched a retry"
    # Still listed: only an actual download takes a track off the list.
    assert len(api.get_failed_tracks()["tracks"]) == 1


# ── webapp.py wiring ──────────────────────────────────────────────────────


def test_single_user_web_resumes_a_job_the_last_process_left(monkeypatch):
    pytest.importorskip("fastapi")
    from SpotiFLAC import webapp

    ran: list[dict] = []
    monkeypatch.setattr(
        SpotiFLAC_API, "run_download_job", lambda self, payload: ran.append(payload)
    )
    # What the previous process left behind: a job that was running when it
    # died.
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (id, owner, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("j1", "", db.dumps({"indices": [], "session": "old"}), "running", 1.0),
        )

    app = webapp.create_app(token=None)

    assert app.state.download_queue is not None
    assert app.state.job_queue is None, "single-user must not look multi-user"
    deadline = time.time() + 3
    while not ran and time.time() < deadline:
        time.sleep(0.01)
    assert ran and ran[0]["session"] == "old"

"""Tests for the versioned REST API mounted at /api/v1 (SpotiFLAC/webapi)."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.webapi import ApiDeps, build_v1_router


class FakeApi:
    """The slice of SpotiFLAC_API the v1 router actually touches."""

    def __init__(self, download_dir: str = "/downloads") -> None:
        self.app_version = "9.9.9"
        self.download_dir = download_dir
        self.fetched: list[str] = []

    def fetch_metadata(self, url: str) -> None:
        self.fetched.append(url)

    def get_extension_health(self) -> dict:
        return {
            "providers": [
                {
                    "provider": "ext:tidal-web",
                    "attempts": 3,
                    "successes": 2,
                    "failures": 1,
                    "success_rate": 2 / 3,
                    "avg_duration_s": 1.5,
                    "last_outcome": "success",
                    "last_error": "",
                    "version": "1.0.0",
                    "installed": True,
                }
            ],
            "totals": {
                "extensions": 1,
                "attempts": 3,
                "successes": 2,
                "failures": 1,
                "success_rate": 2 / 3,
            },
        }


def make_client(**deps_kwargs) -> tuple[TestClient, FakeApi]:
    api = FakeApi(**{k: v for k, v in deps_kwargs.items() if k == "download_dir"})
    deps_kwargs.pop("download_dir", None)
    app = FastAPI()
    app.include_router(
        build_v1_router(ApiDeps(api_for=lambda _request: api, **deps_kwargs))
    )
    return TestClient(app), api


# ── Instance ──────────────────────────────────────────────────────────────


def test_info_reports_the_instance_shape():
    client, _ = make_client()
    body = client.get("/api/v1/info").json()

    assert body["version"] == "9.9.9"
    assert body["api_version"] == "v1"
    assert body["multiuser"] is False
    assert body["authenticated"] is False


def test_info_reports_authentication_when_a_token_is_set():
    client, _ = make_client(token_required=True)
    assert client.get("/api/v1/info").json()["authenticated"] is True


def test_the_openapi_document_describes_every_route():
    client, _ = make_client()
    schema = client.get("/openapi.json").json()

    for path in (
        "/api/v1/info",
        "/api/v1/resolve",
        "/api/v1/downloads",
        "/api/v1/subscriptions",
        "/api/v1/extensions",
    ):
        assert path in schema["paths"], path
    # Declared response models, not free-form dicts — the whole point.
    assert "ResolveResponse" in schema["components"]["schemas"]
    assert "ErrorResponse" in schema["components"]["schemas"]


# ── Validation ────────────────────────────────────────────────────────────


def test_a_blank_url_is_rejected_before_anything_is_attempted():
    client, api = make_client()
    assert client.post("/api/v1/resolve", json={"url": "   "}).status_code == 422
    assert client.post("/api/v1/downloads", json={"url": ""}).status_code == 422
    assert api.fetched == []


def test_search_rejects_an_out_of_range_limit():
    client, _ = make_client()
    assert (
        client.get("/api/v1/search", params={"q": "x", "limit": 999}).status_code == 422
    )
    assert client.get("/api/v1/search", params={"q": ""}).status_code == 422


def test_resolve_reports_a_bad_url_as_a_client_error(monkeypatch):
    class Boom:
        async def get_url_async(self, *_a, **_k):
            raise ValueError("nope")

    monkeypatch.setattr(
        "SpotiFLAC.core.spotify_metadata.SpotifyMetadataClient", lambda *a, **k: Boom()
    )
    client, _ = make_client()
    response = client.post("/api/v1/resolve", json={"url": "https://x.invalid/y"})

    assert response.status_code == 400
    # The internal message never reaches the caller.
    assert "nope" not in response.text


def test_resolve_returns_declared_tracks(monkeypatch):
    track = TrackMetadata(
        id="t1",
        title="Song",
        artists="Artist",
        album="Album",
        album_artist="Artist",
        isrc="ITAAA0000001",
        duration_ms=1000,
    )

    class Fake:
        async def get_url_async(self, url, include_featuring=True):
            return ("An Album", [track], "cover", {})

    monkeypatch.setattr(
        "SpotiFLAC.core.spotify_metadata.SpotifyMetadataClient", lambda *a, **k: Fake()
    )
    client, _ = make_client()
    body = client.post(
        "/api/v1/resolve",
        json={"url": "https://open.spotify.com/album/0000000000000000000000"},
    ).json()

    assert body["name"] == "An Album"
    assert body["kind"] == "album"
    assert body["total"] == 1
    assert body["tracks"][0]["isrc"] == "ITAAA0000001"


# ── Downloads ─────────────────────────────────────────────────────────────


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}
        self.submitted: list[tuple[str, dict]] = []

    def submit(self, owner, payload):
        from SpotiFLAC.core.job_queue import Job

        job = Job(id=f"j{len(self.jobs)}", owner=owner, payload=payload)
        self.jobs[job.id] = job
        self.submitted.append((owner, payload))
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def list_all(self):
        return list(self.jobs.values())

    def list_for(self, owner):
        return [j for j in self.jobs.values() if j.owner == owner]


def test_a_queued_download_answers_202_with_a_job():
    queue = FakeQueue()
    client, _ = make_client(job_queue=queue)

    response = client.post(
        "/api/v1/downloads", json={"url": "https://open.spotify.com/track/x"}
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert queue.submitted[0][1]["url"].endswith("/track/x")


def test_without_a_queue_the_download_is_dispatched_directly():
    client, api = make_client()
    response = client.post(
        "/api/v1/downloads", json={"url": "https://open.spotify.com/track/x"}
    )

    assert response.status_code == 202
    assert response.json()["id"] == "direct"
    assert api.fetched == ["https://open.spotify.com/track/x"]


def test_a_full_queue_answers_429_without_leaking_the_message():
    class FullQueue(FakeQueue):
        def submit(self, owner, payload):
            from SpotiFLAC.core.job_queue import QueueFullError

            raise QueueFullError("internal detail", pending=50, limit=50)

    client, _ = make_client(job_queue=FullQueue())
    response = client.post("/api/v1/downloads", json={"url": "https://x/y"})

    assert response.status_code == 429
    assert "internal detail" not in response.text
    assert "50 queued" in response.text


def test_output_dir_is_ignored_in_multiuser_mode():
    queue = FakeQueue()
    client, _ = make_client(
        job_queue=queue, multiuser=True, username_for=lambda _r: "alice"
    )

    client.post(
        "/api/v1/downloads",
        json={"url": "https://x/y", "output_dir": "/etc"},
    )

    assert "output_dir" not in queue.submitted[0][1]["config"]
    assert queue.submitted[0][0] == "alice"


def test_output_dir_is_honoured_in_single_user_mode():
    queue = FakeQueue()
    client, _ = make_client(job_queue=queue)
    client.post(
        "/api/v1/downloads", json={"url": "https://x/y", "output_dir": "/music"}
    )

    assert queue.submitted[0][1]["config"]["output_dir"] == "/music"


def test_one_account_cannot_read_another_accounts_job():
    queue = FakeQueue()
    client, _ = make_client(
        job_queue=queue, multiuser=True, username_for=lambda _r: "alice"
    )
    client.post("/api/v1/downloads", json={"url": "https://x/y"})

    # Same app, but now every request is bob.
    client2, _ = make_client(
        job_queue=queue, multiuser=True, username_for=lambda _r: "bob"
    )
    assert client2.get("/api/v1/downloads/j0").status_code == 404
    assert client2.get("/api/v1/downloads").json()["jobs"] == []
    # And alice still sees her own.
    assert client.get("/api/v1/downloads/j0").status_code == 200


def test_an_unknown_job_is_404():
    client, _ = make_client(job_queue=FakeQueue())
    assert client.get("/api/v1/downloads/nope").status_code == 404


# ── History ───────────────────────────────────────────────────────────────


def test_history_reports_the_download_log_without_paths():
    from SpotiFLAC.core import download_log

    download_log.record(
        owner="",
        title="Song",
        artist="Artist",
        provider="ext:tidal-web",
        file_path="/home/secret/Music/song.flac",
        fmt="flac",
        size_bytes=10,
    )
    client, _ = make_client()
    body = client.get("/api/v1/history").json()

    assert body["total"] == 1
    assert body["downloads"][0]["title"] == "Song"
    assert "file_path" not in body["downloads"][0]
    assert "/home/secret" not in json.dumps(body)


# ── Subscriptions ─────────────────────────────────────────────────────────


def test_subscriptions_round_trip():
    client, _ = make_client()
    artist = "https://open.spotify.com/artist/0000000000000000000000"

    created = client.post("/api/v1/subscriptions", json={"url": artist, "name": "A"})
    assert created.status_code == 201
    sub_id = created.json()["id"]
    assert created.json()["include_groups"] == "album,single"

    listing = client.get("/api/v1/subscriptions").json()
    assert [s["id"] for s in listing["subscriptions"]] == [sub_id]

    assert client.delete(f"/api/v1/subscriptions/{sub_id}").status_code == 204
    assert client.get("/api/v1/subscriptions").json()["subscriptions"] == []


def test_an_invalid_release_group_is_a_400():
    client, _ = make_client()
    response = client.post(
        "/api/v1/subscriptions",
        json={
            "url": "https://open.spotify.com/artist/0000000000000000000000",
            "include_groups": "bootlegs",
        },
    )
    assert response.status_code == 400
    assert "bootlegs" in response.text


def test_deleting_an_unknown_subscription_is_404():
    client, _ = make_client()
    assert client.delete("/api/v1/subscriptions/nope").status_code == 404


# ── Extensions ────────────────────────────────────────────────────────────


def test_extension_health_is_served_from_the_api_object():
    client, _ = make_client()
    body = client.get("/api/v1/extensions").json()

    assert body["providers"][0]["provider"] == "ext:tidal-web"
    assert body["totals"]["attempts"] == 3


# ── Library ───────────────────────────────────────────────────────────────


def test_library_scan_is_confined_to_the_download_folder(tmp_path):
    client, _ = make_client(download_dir=str(tmp_path))

    outside = client.post(
        "/api/v1/library/scan", json={"path": "/", "target_quality": "LOSSLESS"}
    )
    assert outside.status_code == 400
    assert "outside" in outside.text

    traversal = client.post(
        "/api/v1/library/scan", json={"path": str(tmp_path / ".." / "..")}
    )
    assert traversal.status_code == 400


def test_library_scan_reports_an_empty_folder(tmp_path):
    client, _ = make_client(download_dir=str(tmp_path))
    body = client.post("/api/v1/library/scan", json={"path": str(tmp_path)}).json()

    assert body["scanned"] == 0
    assert body["candidates"] == []
    assert body["target"] == "LOSSLESS"

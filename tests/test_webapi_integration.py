"""End-to-end checks against the real `--web` app, not a hand-built router.

test_webapi_v1.py mounts the v1 router over a fake to test its own logic.
This mounts the actual application webapp.create_app() builds, so it catches
the things that only go wrong in the wiring: a router mounted at the wrong
prefix, an endpoint that the auth middleware accidentally exempts, an admin
route reachable by a plain account.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from SpotiFLAC.core import web_users
from SpotiFLAC.webapp import create_app


@pytest.fixture(autouse=True)
def _isolated_users(tmp_path, monkeypatch):
    monkeypatch.setattr(web_users, "USERS_FILE", tmp_path / "web_users.json")


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


ARTIST = "https://open.spotify.com/artist/0000000000000000000000"


# ── The mounted surface ───────────────────────────────────────────────────


def test_v1_is_mounted_and_documented(client):
    assert client.get("/api/v1/info").status_code == 200

    paths = client.get("/openapi.json").json()["paths"]
    v1 = {p for p in paths if p.startswith("/api/v1")}
    assert {
        "/api/v1/info",
        "/api/v1/resolve",
        "/api/v1/search",
        "/api/v1/downloads",
        "/api/v1/history",
        "/api/v1/subscriptions",
        "/api/v1/subscriptions/check",
        "/api/v1/extensions",
        "/api/v1/library/scan",
    } <= v1


def test_the_legacy_rpc_bridge_still_works(client):
    """The v1 router is additive; the frontend's channel must be untouched."""
    response = client.post("/api/get_version")
    assert response.status_code == 200
    assert "result" in response.json()


def test_subscriptions_round_trip_through_the_real_app(client):
    created = client.post("/api/v1/subscriptions", json={"url": ARTIST, "name": "Test"})
    assert created.status_code == 201
    sub_id = created.json()["id"]

    listing = client.get("/api/v1/subscriptions").json()["subscriptions"]
    assert [s["id"] for s in listing] == [sub_id]
    # The subscription is given this instance's download folder, not a blank.
    assert listing[0]["output_dir"]

    assert client.delete(f"/api/v1/subscriptions/{sub_id}").status_code == 204


def test_extensions_and_history_answer_on_a_fresh_instance(client):
    health = client.get("/api/v1/extensions").json()
    assert "providers" in health and "totals" in health

    history = client.get("/api/v1/history").json()
    assert history == {"total": 0, "downloads": []}


def test_single_user_metrics_are_open(client):
    body = client.get("/api/metrics").json()
    assert body["multiuser"] is False
    # Instance-wide counters are visible when there is only one person.
    assert "providers" in body


# ── Auth ──────────────────────────────────────────────────────────────────


def test_the_token_gate_covers_v1_too():
    with TestClient(create_app(token="s3cret")) as client:
        assert client.get("/api/v1/info").status_code == 401
        assert client.get("/api/v1/info", params={"token": "s3cret"}).status_code == 200
        # /healthz stays exempt, for orchestrators.
        assert client.get("/healthz").status_code == 200


def test_the_session_gate_covers_v1_too():
    web_users.create_user("alice", "pw")
    with TestClient(create_app(multiuser=True)) as client:
        assert client.get("/api/v1/info").status_code == 401

        client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
        assert client.get("/api/v1/info").json()["multiuser"] is True


# ── Admin ─────────────────────────────────────────────────────────────────


def _login(client, username, password="pw"):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def test_admin_routes_are_invisible_to_an_ordinary_account():
    web_users.create_user("bob", "pw")
    with TestClient(create_app(multiuser=True)) as client:
        _login(client, "bob")

        # 404, not 403: whether an admin API exists here is not something an
        # ordinary account should be able to learn.
        assert client.get("/api/admin/users").status_code == 404
        assert client.get("/api/admin/queue").status_code == 404
        assert (
            client.post(
                "/api/admin/role", json={"username": "bob", "role": "admin"}
            ).status_code
            == 404
        )


def test_an_admin_can_read_and_set_quotas():
    web_users.create_user("root", "pw", role="admin")
    web_users.create_user("bob", "pw")

    with TestClient(create_app(multiuser=True)) as client:
        _login(client, "root")

        users = client.get("/api/admin/users").json()["users"]
        assert {u["username"] for u in users} == {"root", "bob"}
        assert all("password_hash" not in u for u in users)
        assert all("usage" in u for u in users)

        assert (
            client.post(
                "/api/admin/quota",
                json={"username": "bob", "daily_track_quota": 5},
            ).status_code
            == 200
        )
        assert web_users.get_user("bob").daily_track_quota == 5

        assert (
            client.post(
                "/api/admin/quota", json={"username": "nobody", "daily_track_quota": 1}
            ).status_code
            == 404
        )


def test_the_last_admin_cannot_be_demoted_over_http():
    web_users.create_user("root", "pw", role="admin")
    with TestClient(create_app(multiuser=True)) as client:
        _login(client, "root")
        response = client.post(
            "/api/admin/role", json={"username": "root", "role": "user"}
        )
        assert response.status_code == 400
        assert "only admin" in response.json()["error"]


def test_metrics_are_admin_only_in_multiuser_mode():
    web_users.create_user("root", "pw", role="admin")
    web_users.create_user("bob", "pw")

    with TestClient(create_app(multiuser=True)) as client:
        _login(client, "bob")
        body = client.get("/api/metrics").json()
        assert body["multiuser"] is True
        # An ordinary account is told nothing about everyone else's activity.
        assert "providers" not in body
        assert "queue" not in body

        client.post("/api/auth/logout")
        _login(client, "root")
        assert "providers" in client.get("/api/metrics").json()


def test_an_account_can_read_its_own_quota():
    web_users.create_user("bob", "pw", daily_track_quota=7)
    with TestClient(create_app(multiuser=True)) as client:
        _login(client, "bob")
        usage = client.get("/api/quota/mine").json()

        assert usage["username"] == "bob"
        assert usage["tracks_limit"] == 7
        assert usage["tracks_used"] == 0


def test_a_download_over_quota_is_refused():
    from SpotiFLAC.core import download_log

    web_users.create_user("bob", "pw", daily_track_quota=1)
    download_log.record(owner="bob", title="Already", file_path="")

    with TestClient(create_app(multiuser=True)) as client:
        _login(client, "bob")
        response = client.post("/api/v1/downloads", json={"url": "https://x/y"})

        assert response.status_code == 429
        assert "quota" in response.text.lower()

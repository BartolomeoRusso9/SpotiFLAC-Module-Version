"""/healthz and /api/metrics — operating a long-running --web instance."""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
webapp = pytest.importorskip("SpotiFLAC.webapp")

TestClient = fastapi_testclient.TestClient


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path))
    from SpotiFLAC.core import web_users

    monkeypatch.setattr(web_users, "USERS_FILE", tmp_path / "web_users.json")


# ── /healthz ───────────────────────────────────────────────────────────────


def test_healthz_answers_without_rendering_the_frontend() -> None:
    client = TestClient(webapp.create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200

    # Liveness only: no version, no client count, no "is auth on" — this is
    # the one endpoint outside the auth gate, so anything it adds is a free
    # fingerprint for whoever can reach the port.
    assert resp.json() == {"status": "ok"}


def test_healthz_is_reachable_without_the_web_token() -> None:
    """A container orchestrator has no token. A probe that 401s reports
    "unhealthy" for a reason unrelated to health.
    """
    client = TestClient(webapp.create_app(token="s3cret"))
    assert client.get("/healthz").status_code == 200


def test_instance_details_live_behind_auth_on_metrics_instead() -> None:
    client = TestClient(webapp.create_app(token="s3cret"))
    body = client.get("/api/metrics?token=s3cret").json()
    assert body["auth"] is True
    assert "version" in body


def test_the_token_gate_still_covers_everything_else() -> None:
    """The /healthz exemption must be exactly one path wide."""
    for path in ("/", "/api/metrics", "/app.js"):
        client = TestClient(webapp.create_app(token="s3cret"))
        assert client.get(path).status_code == 401, path


# ── /api/metrics ───────────────────────────────────────────────────────────


def test_metrics_reports_provider_success_and_failure_counts(tmp_path) -> None:
    """provider_stats has recorded these all along, to order providers by
    reliability. Nothing ever showed them to anyone.
    """
    from SpotiFLAC.core import provider_stats

    provider_stats._save_cache_sync(
        {
            "tidal:https://a.example/api": {
                "successes": 9,
                "failures": 1,
                "last_success": 1.0,
                "last_failure": 0.0,
                "last_attempt": 1.0,
                "last_outcome": "success",
            }
        }
    )

    client = TestClient(webapp.create_app())
    providers = client.get("/api/metrics").json()["providers"]

    assert providers["totals"]["attempts"] == 10
    assert providers["totals"]["success_rate"] == 0.9
    assert providers["providers"]["tidal"]["https://a.example/api"]["successes"] == 9


def test_metrics_is_fine_with_no_history_yet() -> None:
    body = TestClient(webapp.create_app()).get("/api/metrics").json()
    assert body["providers"]["totals"]["attempts"] == 0
    assert body["providers"]["totals"]["success_rate"] is None


def test_metrics_requires_the_token_when_one_is_set() -> None:
    """Unlike /healthz, this exposes real information about the instance."""
    client = TestClient(webapp.create_app(token="s3cret"))
    assert client.get("/api/metrics").status_code == 401
    assert client.get("/api/metrics?token=s3cret").status_code == 200


def test_metrics_requires_a_session_in_multiuser_mode() -> None:
    from SpotiFLAC.core import web_users

    web_users.create_user("alice", "alice-password")
    client = TestClient(webapp.create_app(multiuser=True))

    assert client.get("/api/metrics").status_code == 401

    client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )
    assert client.get("/api/metrics").status_code == 200


def test_metrics_includes_queue_depth_in_multiuser_mode() -> None:
    from SpotiFLAC.core import web_users

    web_users.create_user("alice", "alice-password")
    client = TestClient(webapp.create_app(multiuser=True))
    client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )

    body = client.get("/api/metrics").json()
    assert "queue" in body
    assert body["queue"]["total"] == 0


def test_metrics_has_no_queue_section_without_multiuser() -> None:
    body = TestClient(webapp.create_app()).get("/api/metrics").json()
    assert "queue" not in body

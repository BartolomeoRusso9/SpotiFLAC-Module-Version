"""End-to-end tests for --web-multiuser (login/logout, per-session gating,
queued-download submission/history) — see webapp.py's create_app()
docstring and the SESSION_COOKIE note for exactly what is and isn't
isolated between accounts.
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
webapp = pytest.importorskip("SpotiFLAC.webapp")

TestClient = fastapi_testclient.TestClient
WebSocketDisconnect = pytest.importorskip("starlette.websockets").WebSocketDisconnect


@pytest.fixture(autouse=True)
def _isolated_users_file(tmp_path, monkeypatch):
    from SpotiFLAC.core import web_users

    monkeypatch.setattr(web_users, "USERS_FILE", tmp_path / "web_users.json")
    web_users.create_user("alice", "alice-password")
    web_users.create_user("bob", "bob-password")


def test_auth_status_reports_no_login_needed_when_multiuser_off() -> None:
    app = webapp.create_app()  # multiuser=False, the default
    client = TestClient(app)
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"multiuser": False, "logged_in": True}


def test_auth_status_reflects_login_state_when_multiuser_on() -> None:
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)

    before = client.get("/api/auth/status").json()
    assert before == {"multiuser": True, "logged_in": False}

    client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )
    after = client.get("/api/auth/status").json()
    assert after == {"multiuser": True, "logged_in": True}


def test_auth_status_is_reachable_without_a_session() -> None:
    """/api/auth/status itself must never require a session — otherwise
    the frontend could never learn it needs to show a login screen.
    """
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)
    assert client.get("/api/auth/status").status_code == 200


def test_api_requires_login_when_multiuser_enabled() -> None:
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)

    assert client.post("/api/get_version").status_code == 401
    # The page itself still loads — no login form exists yet in the
    # frontend, so gating it too would leave no way to reach one.
    assert client.get("/").status_code == 200


def test_login_with_wrong_password_is_rejected() -> None:
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)

    resp = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert resp.status_code == 401
    assert client.post("/api/get_version").status_code == 401


def test_login_grants_access_via_session_cookie() -> None:
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)

    resp = client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"
    assert client.cookies.get(webapp.SESSION_COOKIE)

    assert client.post("/api/get_version").status_code == 200


def test_logout_revokes_the_session() -> None:
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)

    client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )
    assert client.post("/api/get_version").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/get_version").status_code == 401


def test_two_users_cannot_use_each_others_sessions() -> None:
    app = webapp.create_app(multiuser=True)
    alice = TestClient(app)
    bob = TestClient(app)

    alice.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )
    bob.post("/api/auth/login", json={"username": "bob", "password": "bob-password"})

    alice_token = alice.cookies.get(webapp.SESSION_COOKIE)
    bob_client_with_alice_cookie = TestClient(app)
    bob_client_with_alice_cookie.cookies.set(webapp.SESSION_COOKIE, "not-alices-token")
    assert bob_client_with_alice_cookie.post("/api/get_version").status_code == 401

    # Sanity: alice's *real* token does work.
    fresh_client = TestClient(app)
    fresh_client.cookies.set(webapp.SESSION_COOKIE, alice_token)
    assert fresh_client.post("/api/get_version").status_code == 200


def test_queue_submit_and_mine_are_scoped_per_user() -> None:
    app = webapp.create_app(multiuser=True)
    alice = TestClient(app)
    bob = TestClient(app)
    alice.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )
    bob.post("/api/auth/login", json={"username": "bob", "password": "bob-password"})

    submit = alice.post(
        "/api/queue/submit-download",
        json={"selected_indices": [0], "config": {}},
    )
    assert submit.status_code == 200
    job_id = submit.json()["job_id"]

    alice_jobs = alice.get("/api/queue/mine").json()["jobs"]
    assert [j["id"] for j in alice_jobs] == [job_id]

    bob_jobs = bob.get("/api/queue/mine").json()["jobs"]
    assert bob_jobs == []


def test_queue_endpoints_require_login() -> None:
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)
    assert (
        client.post(
            "/api/queue/submit-download", json={"selected_indices": [], "config": {}}
        ).status_code
        == 401
    )
    assert client.get("/api/queue/mine").status_code == 401


def test_multiuser_disabled_by_default_has_no_auth_routes() -> None:
    app = webapp.create_app()  # multiuser=False, the default
    client = TestClient(app)

    assert client.post("/api/get_version").status_code == 200
    # No such route when multiuser is off — exactly which "not found"
    # shape FastAPI/Starlette settles on (404 vs. 405 from falling through
    # to the static-files mount) isn't the point; either proves the login
    # endpoint was never registered.
    assert client.post("/api/auth/login", json={}).status_code in (404, 405)


def test_websocket_requires_a_session_when_multiuser_enabled() -> None:
    """Regression: the session gate is HTTP middleware, and Starlette never
    runs @app.middleware("http") for a WebSocket upgrade. /ws carries every
    push event the instance emits (logs, progress, metadata, on-disk paths),
    so without its own check an unauthenticated client could watch
    everything every logged-in account does.

    Asserted on the *connect*, never on a receive: the check runs before
    ws.accept(), so an unauthenticated upgrade is refused outright. Waiting
    for a message instead would hang forever the day this regresses, which
    in CI is worse than a red test.
    """
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
        pass


def test_websocket_accepts_a_logged_in_session() -> None:
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)
    client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password"}
    )
    with client.websocket_connect("/ws"):
        pass  # connecting without being closed is the assertion


def test_repeated_failed_logins_get_rate_limited() -> None:
    """/api/auth/login is the one endpoint reachable without a session, and
    each attempt burns 600k PBKDF2 iterations in Starlette's bounded
    threadpool — so unthrottled it is both a password oracle and a way to
    stall every other request in the process.
    """
    app = webapp.create_app(multiuser=True)
    client = TestClient(app)
    bad = {"username": "alice", "password": "wrong"}

    # A long base delay makes the assertion independent of how fast the
    # machine runs: with the real 1s, six PBKDF2 logins can outlast the
    # first backoff window and the 429 disappears on a slow runner.
    monkeypatch_delay = 3600.0
    webapp.LoginRateLimiter._BASE_DELAY_S = monkeypatch_delay
    try:
        codes = [client.post("/api/auth/login", json=bad).status_code for _ in range(6)]

        assert codes[0] == 401, "the first attempt must not be throttled"
        assert 429 in codes, f"never rate limited after 6 failures: {codes}"
        throttled = client.post("/api/auth/login", json=bad)
        assert throttled.status_code == 429
        assert int(throttled.headers["Retry-After"]) >= 1
    finally:
        webapp.LoginRateLimiter._BASE_DELAY_S = 1.0


def test_login_backoff_is_per_client_and_cleared_by_success() -> None:
    """Unit-level: the endpoint test above proves the wiring, this proves the
    policy — one client's failures must not lock out another, and a correct
    password must clear the penalty rather than leaving it to expire.
    """
    limiter = webapp.LoginRateLimiter()

    for _ in range(6):
        limiter.record_failure("10.0.0.1")

    assert limiter.retry_after("10.0.0.1") is not None
    assert limiter.retry_after("10.0.0.2") is None, "backoff leaked across clients"

    limiter.reset("10.0.0.1")
    assert limiter.retry_after("10.0.0.1") is None


def test_a_few_typos_are_not_punished() -> None:
    limiter = webapp.LoginRateLimiter()
    for _ in range(webapp.LoginRateLimiter._FREE_ATTEMPTS):
        limiter.record_failure("10.0.0.1")
        assert limiter.retry_after("10.0.0.1") is None

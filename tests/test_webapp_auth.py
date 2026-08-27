"""Tests for the optional --web-token shared-secret auth in SpotiFLAC/webapp.py.

Only the auth gate itself is exercised here (via cheap, no-network Api
methods like get_version) — not the rest of the HTTP surface, which needs a
running desktop-equivalent environment to be meaningful.
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
webapp = pytest.importorskip("SpotiFLAC.webapp")

TestClient = fastapi_testclient.TestClient


def test_no_token_configured_allows_everything() -> None:
    app = webapp.create_app(token=None)
    client = TestClient(app)

    assert client.get("/").status_code == 200
    resp = client.post("/api/get_version")
    assert resp.status_code == 200

    with client.websocket_connect("/ws"):
        pass


def test_token_required_when_configured() -> None:
    app = webapp.create_app(token="s3cret")
    client = TestClient(app)

    # No token at all: rejected everywhere.
    assert client.get("/").status_code == 401
    assert client.post("/api/get_version").status_code == 401

    # Wrong token: still rejected.
    assert client.get("/", params={"token": "wrong"}).status_code == 401


def test_correct_query_token_grants_a_cookie_for_later_requests() -> None:
    app = webapp.create_app(token="s3cret")
    client = TestClient(app)

    resp = client.get("/", params={"token": "s3cret"})
    assert resp.status_code == 200
    assert client.cookies.get(webapp.WEB_TOKEN_COOKIE) == "s3cret"

    # No ?token= needed this time — the cookie from the previous request
    # (kept by TestClient's session, exactly like a real browser) covers it.
    resp2 = client.post("/api/get_version")
    assert resp2.status_code == 200


def test_websocket_rejects_missing_or_wrong_token() -> None:
    app = webapp.create_app(token="s3cret")
    client = TestClient(app)

    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass

    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=wrong"):
            pass


def test_websocket_accepts_correct_query_token() -> None:
    app = webapp.create_app(token="s3cret")
    client = TestClient(app)

    with client.websocket_connect("/ws?token=s3cret"):
        pass


def test_resolve_web_token_prefers_explicit_over_env(monkeypatch) -> None:
    monkeypatch.setenv(webapp.WEB_TOKEN_ENV, "from-env")
    assert webapp.resolve_web_token("explicit") == "explicit"
    # None means "not specified on the CLI" -> falls back to the env var.
    assert webapp.resolve_web_token(None) == "from-env"
    # "" is an explicit (if unusual) override, not "unspecified" -> no fallback.
    assert webapp.resolve_web_token("") is None


def test_resolve_web_token_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(webapp.WEB_TOKEN_ENV, raising=False)
    assert webapp.resolve_web_token(None) is None
    assert webapp.resolve_web_token("   ") is None

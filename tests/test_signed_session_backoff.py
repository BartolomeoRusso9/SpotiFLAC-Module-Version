"""A failed verification must not be retried on every track.

runtime_features.signed_fetch() builds a new SignedSessionClient for each
signed request, so the client itself remembers nothing between them. Before
this, a failed Turnstile solve was followed immediately by another — one per
track, per fallback provider, per extension retry — each a fresh /bootstrap
and a fresh challenge, which is how a gateway ends up blocking the address
for hours. These tests pin the pause that now follows a failure, and the
refresh that should keep most sessions from ever needing a new one.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from SpotiFLAC.core import signed_session_mobile as ssm


def _client(tmp_path) -> ssm.SignedSessionClient:
    # A new instance per call, exactly as runtime_features.signed_fetch()
    # does — the pause has to survive that to be worth anything.
    return ssm.SignedSessionClient(
        base_url="https://gateway.invalid/v2",
        namespace="zarz-v2",
        app_version="tidal-web@1.2.5",
        platform="extension",
        endpoints={"refresh": "/session/refresh"},
        data_dir=str(tmp_path),
    )


class _Response:
    status_code = 200
    headers: dict = {}
    url = "https://gateway.invalid/v2/dl/tid"
    text = "{}"
    content = b"{}"


def _fetch(client, **kwargs) -> dict:
    return asyncio.run(
        ssm.perform_signed_fetch(client, "POST", "/dl/tid", {}, {}, **kwargs)
    )


def _failing_auth(calls: list, exc: BaseException):
    async def _auth(self, *args, **kwargs):
        calls.append(1)
        raise exc

    return _auth


def test_a_failed_verification_is_not_retried_by_the_next_request(
    tmp_path, monkeypatch
) -> None:
    calls: list = []
    monkeypatch.setattr(
        ssm.SignedSessionClient,
        "authenticate_with_turnstile",
        _failing_auth(calls, TimeoutError("Turnstile token not obtained")),
    )

    first = _fetch(_client(tmp_path))
    second = _fetch(_client(tmp_path))
    third = _fetch(_client(tmp_path))

    assert len(calls) == 1
    assert "TimeoutError" in first["error"]
    assert "paused" in second["error"]
    assert "paused" in third["error"]


def test_the_pause_grows_with_each_consecutive_failure(tmp_path) -> None:
    client = _client(tmp_path)
    first = ssm._record_auth_failure(client)
    second = ssm._record_auth_failure(client)
    third = ssm._record_auth_failure(client)
    assert first == ssm._AUTH_BACKOFF_BASE_S
    assert second == first * ssm._AUTH_BACKOFF_FACTOR
    assert third == second * ssm._AUTH_BACKOFF_FACTOR
    for _ in range(10):
        last = ssm._record_auth_failure(client)
    assert last == ssm._AUTH_BACKOFF_MAX_S


def test_the_gateway_s_own_retry_after_is_honoured_uncapped(
    tmp_path, monkeypatch
) -> None:
    """The case that prompted this: a 429 asking for 17 hours."""
    seventeen_hours = 17 * 3600
    request = httpx.Request("GET", "https://gateway.invalid/v2/bootstrap")
    response = httpx.Response(
        429, headers={"Retry-After": str(seventeen_hours)}, request=request
    )
    refusal = httpx.HTTPStatusError("429", request=request, response=response)
    monkeypatch.setattr(
        ssm.SignedSessionClient,
        "authenticate_with_turnstile",
        _failing_auth([], refusal),
    )

    _fetch(_client(tmp_path))

    remaining = ssm.auth_backoff_remaining(_client(tmp_path))
    assert seventeen_hours - 5 < remaining <= seventeen_hours


def test_retry_after_is_read_from_the_error_envelope_too() -> None:
    request = httpx.Request("GET", "https://gateway.invalid/v2/bootstrap")
    response = httpx.Response(
        429,
        json={"code": "RATE_LIMITED", "origin": "gateway", "retry_after_seconds": 900},
        request=request,
    )
    exc = httpx.HTTPStatusError("429", request=request, response=response)
    assert ssm._retry_after_from(exc) == 900
    assert ssm._retry_after_from(TimeoutError()) == 0


def test_a_successful_verification_clears_the_pause(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path)
    ssm._record_auth_failure(client)
    # The pause has run out, but the failure count is still on disk.
    path = ssm._auth_backoff_path(client)
    path.write_text('{"failures": 3, "not_before": %f}' % (time.time() - 1))

    async def _auth(self, *args, **kwargs):
        self.session_id = "sess"
        self.session_secret = "secret"
        self.expires_at = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()

    async def _request(self, *args, **kwargs):
        return _Response()

    monkeypatch.setattr(ssm.SignedSessionClient, "authenticate_with_turnstile", _auth)
    monkeypatch.setattr(ssm.SignedSessionClient, "request", _request)

    result = _fetch(_client(tmp_path))

    assert result["ok"] is True
    assert not path.exists()


def test_the_pause_file_is_not_a_session_file(tmp_path) -> None:
    client = _client(tmp_path)
    ssm._record_auth_failure(client)
    assert ssm._auth_backoff_path(client).name.startswith(".")
    assert ssm._auth_backoff_path(client) != client._path


# --- refresh ----------------------------------------------------------------


class _RecordingHttp:
    def __init__(self, response: httpx.Response | None = None, error=None):
        self.calls: list[dict] = []
        self._response = response
        self._error = error

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._error is not None:
            raise self._error
        return self._response

    async def aclose(self):
        pass


def _session_client(tmp_path) -> ssm.SignedSessionClient:
    client = _client(tmp_path)
    client.session_id = "sess_abc"
    client.session_secret = "secret"
    client.expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return client


@pytest.fixture(autouse=True)
def _no_refresh_throttle_between_tests():
    ssm._REFRESH_RETRY_AT.clear()
    ssm._REFRESH_DONE_AT.clear()
    yield
    ssm._REFRESH_RETRY_AT.clear()
    ssm._REFRESH_DONE_AT.clear()


def test_refresh_signs_the_bytes_it_actually_sends(tmp_path) -> None:
    """Regression: it hashed json.dumps(body) and sent httpx's compact form."""
    client = _session_client(tmp_path)
    request = httpx.Request("POST", "https://gateway.invalid/v2/session/refresh")
    new_expiry = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    http = _RecordingHttp(
        httpx.Response(200, json={"expires_at": new_expiry}, request=request)
    )
    client._client = http

    asyncio.run(client._refresh())

    sent = http.calls[0]
    assert "json" not in sent
    body = sent["content"]
    assert sent["headers"]["X-Sig-Body-Sha256"] == hashlib.sha256(body).hexdigest()
    assert client.expires_at == new_expiry


def test_a_refused_refresh_keeps_the_session_and_is_not_repeated(tmp_path) -> None:
    client = _session_client(tmp_path)
    request = httpx.Request("POST", "https://gateway.invalid/v2/session/refresh")
    http = _RecordingHttp(httpx.Response(401, text="nope", request=request))
    client._client = http

    asyncio.run(client._refresh())
    asyncio.run(client._refresh())

    assert len(http.calls) == 1
    assert client.authenticated


def test_a_refresh_that_cannot_connect_does_not_fail_the_request(tmp_path) -> None:
    client = _session_client(tmp_path)
    client._client = _RecordingHttp(error=httpx.ConnectError("down"))

    asyncio.run(client._refresh())  # must not raise

    assert client.authenticated


@pytest.mark.parametrize("body", ["<html>maintenance</html>", "[]"])
def test_a_200_without_a_session_is_a_failed_refresh(tmp_path, body) -> None:
    client = _session_client(tmp_path)
    request = httpx.Request("POST", "https://gateway.invalid/v2/session/refresh")
    http = _RecordingHttp(httpx.Response(200, text=body, request=request))
    client._client = http
    expires = client.expires_at

    asyncio.run(client._refresh())
    asyncio.run(client._refresh())

    assert len(http.calls) == 1, "taken as a refresh, it was re-posted every time"
    assert client.expires_at == expires


def test_concurrent_refreshes_post_once(tmp_path) -> None:
    """Two requests past refresh_after, each with its own client, as the
    runtime builds them: only the first may post."""
    new_expiry = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    posts: list[str] = []

    class _SlowHttp:
        async def post(self, url, **kwargs):
            posts.append(url)
            await asyncio.sleep(0.2)
            return httpx.Response(
                200,
                json={"expires_at": new_expiry, "refresh_after": new_expiry},
                request=httpx.Request("POST", url),
            )

        async def aclose(self):
            pass

    first, second = _session_client(tmp_path), _session_client(tmp_path)
    assert first._path == second._path
    first._client, second._client = _SlowHttp(), _SlowHttp()

    async def _both():
        await asyncio.gather(first._refresh(), second._refresh())

    asyncio.run(_both())

    assert len(posts) == 1


def test_a_pause_is_per_gateway_and_shared_by_its_extensions(tmp_path) -> None:
    tidal = _client(tmp_path)
    qobuz = ssm.SignedSessionClient(
        base_url="https://gateway.invalid/v2",
        namespace="zarz-v2",
        app_version="qobuz-web@1.2.15",
        platform="extension",
        data_dir=str(tmp_path),
    )
    elsewhere = ssm.SignedSessionClient(
        base_url="https://other-gateway.invalid/v2",
        namespace="zarz-v2",
        app_version="tidal-web@1.2.5",
        platform="extension",
        data_dir=str(tmp_path),
    )

    ssm._record_auth_failure(tidal)

    assert ssm.auth_backoff_remaining(qobuz) > 0, "same gateway, same address"
    assert ssm.auth_backoff_remaining(elsewhere) == 0


def test_a_timed_out_request_says_so_instead_of_returning_an_empty_error(
    tmp_path, caplog
) -> None:
    """`str(exc)` is "" for every httpx timeout class.

    That emptiness reached both sides of the bridge: the log line read
    "... failed:" and stopped, and `{"error": ""}` is falsy, so an
    extension's `if (response.error)` branch was skipped and it threw
    "HTTP undefined for /dl/tid" — losing the reason, and the Retry-After
    with it. A /dl that stalls for its full 30 seconds is the most common
    way a signed request ends here, so it is the one that has to be legible.
    """
    client = _client(tmp_path)
    client.session_id = "sid"
    client.session_secret = "secret"
    client.expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    async def _timeout(*args, **kwargs):
        raise httpx.ReadTimeout("")

    client.request = _timeout

    with caplog.at_level("WARNING", logger="SpotiFLAC.core.signed_session_mobile"):
        result = _fetch(client)

    assert "ReadTimeout" in result["error"]
    assert "ReadTimeout" in caplog.text


def _live(client) -> None:
    """Gives *client* a session that ensure_session() will accept."""
    client.session_id = "sid"
    client.session_secret = "secret"
    client.expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _Envelope:
    """A gateway refusal carrying the error envelope of signed_session_errors."""

    url = "https://gateway.invalid/v2/dl/tid"

    def __init__(self, status: int, body: str, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body.encode()
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._body.decode()

    @property
    def content(self) -> bytes:
        return self._body

    def json(self):
        import json

        return json.loads(self._body)


def _answered(client, response) -> dict:
    async def _respond(*args, **kwargs):
        return response

    client.request = _respond
    return _fetch(client)


BUSY = (
    '{"error":"operation running","code":"TICKET_BUSY","origin":"gateway",'
    '"retryable":true,"retry_mode":"poll_existing","retry_after_seconds":4}'
)


def test_the_gateway_s_retry_mode_reaches_the_extension(tmp_path) -> None:
    """The contract was parsed, acted on, and never carried across.

    `signed_session_errors.parse_session_error` reads `retry_mode`, and
    tidal-web branches on it — `poll_existing` is the case where the ticket
    owns an operation already running and must be kept rather than thrown
    away (index.js). This function returned a dict without the field, so
    every refusal read as "no instruction given" and every retry started a
    fresh operation with a fresh ticket.
    """
    client = _client(tmp_path)
    _live(client)

    result = _answered(client, _Envelope(409, BUSY))

    assert result["retryMode"] == "poll_existing"
    assert result["retryAfterSeconds"] == 4


def test_code_and_retryable_are_deliberately_withheld(tmp_path) -> None:
    """They come out of the same envelope and are left where they are.

    The extensions read `code && !retryable` as "stop retrying", so
    forwarding those two would hand the gateway a switch that ends a
    download early. Whether it sets them that precisely on a transient
    failure is not something this side can verify, so the narrow field is
    the one that travels.
    """
    client = _client(tmp_path)
    _live(client)

    result = _answered(client, _Envelope(409, BUSY))

    assert "code" not in result
    assert "retryable" not in result


def test_a_retry_after_header_still_wins_over_the_envelope(tmp_path) -> None:
    client = _client(tmp_path)
    _live(client)

    result = _answered(client, _Envelope(429, BUSY, {"Retry-After": "9"}))

    assert result["retryAfterSeconds"] == 9


def test_a_success_body_is_never_parsed_as_an_envelope(tmp_path) -> None:
    """A 200 carries the provider's payload — sometimes a large manifest."""
    client = _client(tmp_path)
    _live(client)

    result = _answered(client, _Envelope(200, '{"data":{"manifest":"<MPD/>"}}'))

    assert result["ok"] is True
    assert "retryMode" not in result


def test_a_refusal_without_an_envelope_adds_nothing(tmp_path) -> None:
    client = _client(tmp_path)
    _live(client)

    result = _answered(client, _Envelope(500, "upstream exploded"))

    assert result["ok"] is False
    assert "retryMode" not in result


def test_the_request_timeout_is_per_phase_and_configurable(tmp_path) -> None:
    """A flat `timeout=30` gave connect, read, write and pool 30s each.

    Only the read is worth that: the gateway proxies to a provider. A
    connect still unfinished after 10s is not going to finish, and a 30s
    pool wait means the limits are wrong rather than the network slow.
    """
    client = _client(tmp_path)
    assert client.request_timeout == ssm._DEFAULT_REQUEST_TIMEOUT_S

    timeout = client._timeout()
    assert timeout.connect == ssm._CONNECT_TIMEOUT_S
    assert timeout.pool == ssm._POOL_TIMEOUT_S
    assert timeout.read == ssm._DEFAULT_REQUEST_TIMEOUT_S

    # A refresh is a gateway-local call and keeps its own shorter wait.
    assert client._timeout(read=15).read == 15


def test_a_manifest_may_declare_its_own_request_timeout(tmp_path) -> None:
    block = {
        "baseUrl": "https://gateway.invalid/v2",
        "namespace": "zarz-v2",
        "requestTimeoutSeconds": 75,
    }
    client = ssm.client_from_manifest(block, data_dir=str(tmp_path))
    assert client.request_timeout == 75.0
    assert client._timeout().read == 75.0

    block["requestTimeoutSeconds"] = "not a number"
    fallback = ssm.client_from_manifest(block, data_dir=str(tmp_path))
    assert fallback.request_timeout == ssm._DEFAULT_REQUEST_TIMEOUT_S

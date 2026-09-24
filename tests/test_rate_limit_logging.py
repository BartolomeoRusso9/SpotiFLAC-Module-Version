"""A 429 is logged with the wait it was given.

The extensions — the JS ones above all — retry a rate limit on their own and
say nothing about it, so the log held no record of how long an address had
been limited. A watcher on the host that moves the downloads to a VPN for
exactly that long had nothing to read. Each place a 429 reaches the host now
logs it with its Retry-After: the signed-session gateway (mobile), the
desktop verification flow (in the error it raises) and the Node bridge the JS
extensions make their requests through.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
import json
import shutil
import threading
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time

import pytest

from SpotiFLAC.core import signed_session_desktop as ssd
from SpotiFLAC.core import signed_session_mobile as ssm
from SpotiFLAC.extensions.runtime import JSRuntime

# ---------------------------------------------------------------------------
# signed session (mobile): perform_signed_fetch
# ---------------------------------------------------------------------------


class _Response:
    url = "https://gateway.invalid/v2/dl/tid"

    def __init__(
        self, status: int, body: str = "", headers: dict | None = None
    ) -> None:
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
        return json.loads(self._body)


def _fetch(tmp_path, response: _Response, path: str = "/dl/tid") -> dict:
    client = ssm.SignedSessionClient(
        base_url="https://gateway.invalid/v2",
        namespace="zarz-v2",
        app_version="tidal-web@1.2.5",
        platform="extension",
        data_dir=str(tmp_path),
    )
    client.session_id = "sid"
    client.session_secret = "secret"
    client.expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    async def _respond(*args, **kwargs):
        return response

    cast(Any, client).request = _respond
    return asyncio.run(ssm.perform_signed_fetch(client, "POST", path, {}, {}))


LIMITED = (
    '{"error":"rate limited","code":"RATE_LIMITED","origin":"gateway",'
    '"retryable":true,"retry_after_seconds":900}'
)


def test_a_429_is_logged_with_the_retry_after_header(tmp_path, caplog) -> None:
    with caplog.at_level("WARNING", logger="SpotiFLAC.core.signed_session_mobile"):
        result = _fetch(tmp_path, _Response(429, "", {"Retry-After": "30"}))

    assert "HTTP 429 for POST /dl/tid — retry after 30s" in caplog.text
    assert result["retryAfterSeconds"] == 30


def test_the_wait_comes_from_the_envelope_when_there_is_no_header(
    tmp_path, caplog
) -> None:
    with caplog.at_level("WARNING", logger="SpotiFLAC.core.signed_session_mobile"):
        result = _fetch(tmp_path, _Response(429, LIMITED), path="/tickets")

    assert "HTTP 429 for POST /tickets — retry after 900s" in caplog.text
    assert result["retryAfterSeconds"] == 900


def test_an_http_date_header_is_turned_into_seconds(tmp_path, caplog) -> None:
    """The date form of Retry-After, which the Node bridge has always read."""
    header = formatdate(time() + 600, usegmt=True)
    with caplog.at_level("WARNING", logger="SpotiFLAC.core.signed_session_mobile"):
        result = _fetch(tmp_path, _Response(429, "", {"Retry-After": header}))

    assert 590 <= result["retryAfterSeconds"] <= 600
    assert f"retry after {result['retryAfterSeconds']}s" in caplog.text


def test_a_past_http_date_falls_back_to_the_envelope(tmp_path, caplog) -> None:
    """A wait already over is no wait — the envelope still has one."""
    header = formatdate(time() - 600, usegmt=True)
    with caplog.at_level("WARNING", logger="SpotiFLAC.core.signed_session_mobile"):
        result = _fetch(tmp_path, _Response(429, LIMITED, {"Retry-After": header}))

    assert "HTTP 429 for POST /dl/tid — retry after 900s" in caplog.text
    assert result["retryAfterSeconds"] == 900


def test_a_429_without_any_wait_says_so(tmp_path, caplog) -> None:
    with caplog.at_level("WARNING", logger="SpotiFLAC.core.signed_session_mobile"):
        _fetch(tmp_path, _Response(429, "slow down"))

    assert "HTTP 429 for POST /dl/tid — no Retry-After" in caplog.text


def test_other_refusals_add_no_rate_limit_line(tmp_path, caplog) -> None:
    with caplog.at_level("WARNING", logger="SpotiFLAC.core.signed_session_mobile"):
        result = _fetch(tmp_path, _Response(409, LIMITED, {"Retry-After": "4"}))

    assert "HTTP 429" not in caplog.text
    # What reaches the extension is unchanged.
    assert result["retryAfterSeconds"] == 4


# ---------------------------------------------------------------------------
# signed session (desktop): the errors raised from its HTTP calls
# ---------------------------------------------------------------------------


class _RequestsResponse:
    def __init__(
        self, status: int, body: bytes = b"", headers: dict | None = None
    ) -> None:
        self.status_code = status
        self.content = body
        self.headers = headers or {}


def test_a_desktop_refusal_carries_the_retry_after_header() -> None:
    resp = _RequestsResponse(429, b"", {"Retry-After": "120"})
    assert (
        ssd._refusal_message("session exchange", resp)
        == "session exchange returned HTTP 429, retry after 120s"
    )


def test_a_desktop_refusal_reads_an_http_date_header() -> None:
    resp = _RequestsResponse(
        429, b"", {"Retry-After": formatdate(time() + 600, usegmt=True)}
    )
    seconds = int(
        ssd._refusal_message("session exchange", resp)
        .rsplit("retry after ", 1)[1]
        .rstrip("s")
    )
    assert 590 <= seconds <= 600


def test_a_desktop_refusal_ignores_an_unparsable_header() -> None:
    """Garbage in the header is not a wait; the envelope is still read."""
    resp = _RequestsResponse(429, LIMITED.encode(), {"Retry-After": "soon"})
    assert (
        ssd._refusal_message("session exchange", resp)
        == "session exchange returned HTTP 429, retry after 900s"
    )


def test_a_desktop_refusal_reads_the_wait_from_the_envelope() -> None:
    resp = _RequestsResponse(429, LIMITED.encode())
    assert (
        ssd._refusal_message("verification bootstrap", resp)
        == "verification bootstrap returned HTTP 429, retry after 900s"
    )


def test_a_desktop_refusal_without_a_wait_keeps_the_old_wording() -> None:
    resp = _RequestsResponse(503, b"<html>down</html>")
    assert (
        ssd._refusal_message("session exchange", resp)
        == "session exchange returned HTTP 503"
    )


# ---------------------------------------------------------------------------
# Node bridge: the JS extensions' own requests
# ---------------------------------------------------------------------------

PROBE = """
registerExtension({
  initialize: function () {},
  run: function (source) { return eval(source); }
});
"""


class _Limited(BaseHTTPRequestHandler):
    def do_GET(self):
        headers = {}
        if self.path == "/seconds":
            headers["Retry-After"] = "45"
        elif self.path == "/date":
            headers["Retry-After"] = formatdate(time() + 600, usegmt=True)
        status = 200 if self.path == "/fine" else 429
        body = b"ok" if status == 200 else b"slow down"
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def limited_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Limited)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("needs Node to exercise the real bridge")
    # The test server is on loopback, which the extension network guard refuses.
    monkeypatch.setenv("SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK", "1")
    ext = tmp_path / "index.js"
    ext.write_text(PROBE)
    rt = JSRuntime(ext_path=ext)
    rt.start()
    try:
        yield lambda source: rt.call("run", source)
    finally:
        rt.stop()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "HTTP 429" in r.getMessage()]


def test_the_bridge_logs_a_429_with_its_retry_after(
    bridge, limited_server, caplog
) -> None:
    host = limited_server.removeprefix("http://")
    with caplog.at_level("WARNING", logger="SpotiFLAC.extensions.runtime"):
        res = bridge(f"http.get({json.dumps(limited_server + '/seconds')})")

    assert res["status"] == 429  # the extension still gets the response
    assert _warnings(caplog) == [
        f"[EXT] [http] HTTP 429 from {host} (GET) — retry after 45s"
    ]


def test_an_http_date_retry_after_is_turned_into_seconds(
    bridge, limited_server, caplog
) -> None:
    with caplog.at_level("WARNING", logger="SpotiFLAC.extensions.runtime"):
        bridge(
            f"http.request({json.dumps(limited_server + '/date')}, {{method: 'GET'}})"
        )

    [line] = _warnings(caplog)
    seconds = int(line.rsplit("retry after ", 1)[1].rstrip("s"))
    assert 590 <= seconds <= 600


def test_a_429_without_retry_after_is_still_logged(
    bridge, limited_server, caplog
) -> None:
    with caplog.at_level("WARNING", logger="SpotiFLAC.extensions.runtime"):
        bridge(f"http.get({json.dumps(limited_server + '/none')})")

    [line] = _warnings(caplog)
    assert line.endswith("— no Retry-After")


def test_a_successful_request_logs_nothing(bridge, limited_server, caplog) -> None:
    with caplog.at_level("WARNING", logger="SpotiFLAC.extensions.runtime"):
        res = bridge(f"http.get({json.dumps(limited_server + '/fine')})")

    assert res["status"] == 200
    assert _warnings(caplog) == []

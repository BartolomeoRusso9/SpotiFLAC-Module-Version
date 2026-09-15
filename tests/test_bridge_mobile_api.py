"""The parts of the mobile app's extension API the bridge was missing.

spotify-web and apple-music, from the mobile app's own extension registry,
call `utils.hmacSHA1`, `http.request`, `matching.compareStrings` /
`compareDuration`, `gobackend.getLocalTime` and `utils.isRequestCancelled`.
Without the first, spotify-web failed every call ("utils.hmacSHA1 is not a
function"); without `matching`, apple-music's result scoring threw.

The reference is the mobile runtime's Go source. What is pinned here is
where a plausible Node version would differ from it: the HMAC's return
shape, a Levenshtein counted over UTF-8 bytes rather than characters, a
duration comparison that answers a boolean, and a request body's default
Content-Type. These drive the real _bridge.js under the real Node.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from SpotiFLAC.extensions.runtime import JSRuntime

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node to exercise the real bridge"
)

PROBE = """
registerExtension({
  initialize: function () {},
  run: function (source) { return eval(source); }
});
"""


class _Echo(BaseHTTPRequestHandler):
    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if self.path in ("/r302", "/r307", "/r308"):
            self.send_response(int(self.path[2:]))
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(
            {
                "method": self.command,
                "body": body,
                "type": self.headers.get("Content-Type"),
                "x": self.headers.get("X-Test"),
            }
        ).encode()
        self.send_response(201 if self.command == "PUT" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _answer

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    ext = tmp_path_factory.mktemp("probe") / "index.js"
    ext.write_text(PROBE)
    state = {"cancelled": False}
    rt = JSRuntime(ext_path=ext, cancelled_probe=lambda: state["cancelled"])
    rt.start()
    try:
        yield (lambda source: rt.call("run", source)), state
    finally:
        rt.stop()


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# utils.hmacSHA1
# ---------------------------------------------------------------------------


def test_hmac_sha1_answers_byte_values_like_the_mobile_runtime(js) -> None:
    run, _ = js
    # RFC 2202, test case 2.
    expected = list(
        hmac.new(b"Jefe", b"what do ya want for nothing?", hashlib.sha1).digest()
    )
    assert run('utils.hmacSHA1("Jefe", "what do ya want for nothing?")') == expected


def test_hmac_sha1_takes_byte_arrays_the_way_totp_passes_them(js) -> None:
    run, _ = js
    key = [1, 2, 3, 250]
    counter = [0, 0, 0, 0, 3, 141, 126, 64]
    expected = list(hmac.new(bytes(key), bytes(counter), hashlib.sha1).digest())
    assert run(f"utils.hmacSHA1({key}, {counter})") == expected
    # And through gobackend, which is the same object here.
    assert run(f"gobackend.hmacSHA1({key}, {counter})") == expected


def test_hmac_sha1_refuses_what_mobile_refuses(js) -> None:
    run, _ = js
    assert run("utils.hmacSHA1({}, 'x')") == []


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def test_compare_strings_is_a_levenshtein_ratio(js) -> None:
    run, _ = js
    assert run("matching.compareStrings('Hello ', 'hello')") == 1
    assert run("matching.compareStrings('hello', 'hallo')") == pytest.approx(0.8)
    assert run("matching.compareStrings('', 'x')") == 0
    assert run("matching.compareStrings('only one')") == 0


def test_compare_strings_counts_bytes_as_the_go_version_does(js) -> None:
    """Go's len() and indexing are over bytes, so a differing Hangul
    syllable costs the bytes that differ, out of the byte length."""
    run, _ = js
    ours = run("matching.compareStrings('밤편지', '밤편자')")
    a, b = "밤편지".encode(), "밤편자".encode()
    differing = sum(x != y for x, y in zip(a, b))
    assert ours == pytest.approx(1 - differing / max(len(a), len(b)))
    assert ours != pytest.approx(1 - 1 / 3)  # not the per-character ratio


def test_compare_duration_is_a_boolean_with_a_three_second_default(js) -> None:
    run, _ = js
    assert run("matching.compareDuration(200000, 203000)") is True
    assert run("matching.compareDuration(200000, 203001)") is False
    assert run("matching.compareDuration(200000, 205000, 5000)") is True
    assert run("matching.compareDuration(1)") is False


def test_normalize_string_strips_suffixes_and_punctuation(js) -> None:
    run, _ = js
    assert run("matching.normalizeString('Everlong (Remastered)')") == "everlong"
    assert run("matching.normalizeString('Song  feat. Someone!')") == "song"
    assert run('matching.normalizeString("Don\'t Stop")') == "dont stop"


# ---------------------------------------------------------------------------
# gobackend.getLocalTime, utils.isRequestCancelled
# ---------------------------------------------------------------------------


def test_local_time_has_the_mobile_fields(js) -> None:
    run, _ = js
    now = run("gobackend.getLocalTime()")
    assert set(now) == {
        "year", "month", "day", "hour", "minute", "second",
        "weekday", "offsetMinutes", "timezone", "timestamp",
    }  # fmt: skip
    assert 1 <= now["month"] <= 12 and 0 <= now["weekday"] <= 6
    assert run("new Date().getTimezoneOffset()") == now["offsetMinutes"]


def test_request_cancellation_is_the_host_s_stop_event(js) -> None:
    run, state = js
    assert run("utils.isRequestCancelled()") is False
    state["cancelled"] = True
    try:
        # Past the bridge's short answer cache.
        run("utils.sleep(300)")
        assert run("utils.isRequestCancelled()") is True
    finally:
        state["cancelled"] = False
        run("utils.sleep(300)")


# ---------------------------------------------------------------------------
# http.request and the method shortcuts
# ---------------------------------------------------------------------------


@pytest.fixture
def private_network(monkeypatch):
    # The echo server is on loopback, which the extension network guard
    # refuses; the runtime above was started before this could apply, so
    # these tests use a runtime of their own.
    monkeypatch.setenv("SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK", "1")


@pytest.fixture
def net_js(tmp_path: Path, private_network):
    ext = tmp_path / "index.js"
    ext.write_text(PROBE)
    rt = JSRuntime(ext_path=ext)
    rt.start()
    try:
        yield lambda source: rt.call("run", source)
    finally:
        rt.stop()


def test_request_sends_the_method_body_and_headers(net_js, server) -> None:
    res = net_js(
        f"http.request({json.dumps(server)}, "
        "{method: 'post', body: {a: 1}, headers: {'X-Test': 'yes'}})"
    )
    assert res["ok"] is True
    assert json.loads(res["body"]) == {
        "method": "POST",
        "body": '{"a":1}',
        "type": "application/json",  # defaulted, as on mobile
        "x": "yes",
    }


def test_a_head_request_has_no_body_and_still_a_status(net_js, server) -> None:
    res = net_js(f"http.request({json.dumps(server)}, {{method: 'HEAD'}})")
    assert (res["status"], res["body"]) == (200, "")


def test_an_explicit_content_type_is_kept(net_js, server) -> None:
    res = net_js(
        f"http.post({json.dumps(server)}, 'a=1', "
        "{'Content-Type': 'application/x-www-form-urlencoded'})"
    )
    assert json.loads(res["body"])["type"] == "application/x-www-form-urlencoded"


def test_put_patch_and_delete_shortcuts(net_js, server) -> None:
    url = json.dumps(server)
    put = net_js(f"http.put({url}, 'x', {{}})")
    assert (put["status"], json.loads(put["body"])["method"]) == (201, "PUT")
    assert json.loads(net_js(f"http.patch({url}, 'x')")["body"])["method"] == "PATCH"
    gone = json.loads(net_js(f"http.delete({url}, {{'X-Test': 'd'}})")["body"])
    assert (gone["method"], gone["x"], gone["body"]) == ("DELETE", "d", "")


def test_307_and_308_repeat_the_request_302_turns_it_into_a_get(net_js, server) -> None:
    for code in ("307", "308"):
        moved = json.loads(
            net_js(f"http.post({json.dumps(server + '/r' + code)}, 'a=1')")["body"]
        )
        assert (moved["method"], moved["body"]) == ("POST", "a=1"), code
        put = json.loads(
            net_js(f"http.put({json.dumps(server + '/r' + code)}, 'p')")["body"]
        )
        assert (put["method"], put["body"]) == ("PUT", "p"), code

    found = json.loads(
        net_js(f"http.post({json.dumps(server + '/r302')}, 'a=1')")["body"]
    )
    assert (found["method"], found["body"]) == ("GET", "")

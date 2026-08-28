"""Resuming an interrupted download instead of restarting it.

stream_to_file() already wrote to a `.part` file; it just always started
from byte zero. These tests drive it against a real local HTTP server so
the Range negotiation is exercised end to end, including the two ways a
server can decline to honour it.
"""

from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

from SpotiFLAC.core.http import AsyncHttpClient
from SpotiFLAC.core.loop_runner import run_sync

pytest.importorskip("aiofiles")

BODY = bytes(range(256)) * 40  # 10240 bytes, position-dependent content


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves BODY, honouring Range unless the server says otherwise."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - stdlib naming
        mode = self.server.range_mode  # type: ignore[attr-defined]
        self.server.requests.append(self.headers.get("Range"))  # type: ignore[attr-defined]

        rng = self.headers.get("Range")
        if rng and mode == "honour":
            start = int(rng.removeprefix("bytes=").split("-")[0])
            if start >= len(BODY):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(BODY)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            chunk = BODY[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"
            )
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return

        # No Range asked for, or a server that ignores it: whole body, 200.
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _RangeHandler)
    srv.daemon_threads = True
    srv.range_mode = "honour"
    srv.requests = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/audio.flac"
    yield srv
    srv.shutdown()


@pytest.fixture
def client():
    return AsyncHttpClient("test", timeout_s=10)


def _fetch(client, server, dest, **kwargs):
    run_sync(client.stream_to_file(server.url, str(dest), **kwargs))


def test_a_plain_download_still_works(client, server, tmp_path) -> None:
    dest = tmp_path / "audio.flac"
    _fetch(client, server, dest)
    assert dest.read_bytes() == BODY
    assert server.requests == [None], "a fresh download must not send Range"


def test_an_interrupted_download_resumes_from_where_it_stopped(
    client, server, tmp_path
) -> None:
    dest = tmp_path / "audio.flac"
    partial = tmp_path / "audio.flac.part"
    partial.write_bytes(BODY[:4000])

    _fetch(client, server, dest)

    assert server.requests == ["bytes=4000-"]
    assert dest.read_bytes() == BODY, "resumed file does not match the original"
    assert not partial.exists()


def test_the_part_file_survives_a_failure_so_the_next_run_can_resume(
    client, server, tmp_path
) -> None:
    """The point of the whole feature: killing the transfer must leave the
    bytes already on disk behind, not delete them.
    """
    import asyncio

    dest = tmp_path / "audio.flac"
    stop = None

    async def run_and_stop():
        nonlocal stop
        stop = asyncio.Event()

        def on_progress(done, total):
            if done > 0:
                stop.set()

        with pytest.raises(Exception):
            await client.stream_to_file(
                server.url,
                str(dest),
                progress_cb=on_progress,
                chunk_size=1024,
                stop_event=stop,
            )

    run_sync(run_and_stop())

    partial = tmp_path / "audio.flac.part"
    assert partial.exists(), "cancelling deleted the partial file"
    assert 0 < partial.stat().st_size < len(BODY)

    # ...and a second attempt completes it.
    _fetch(client, server, dest)
    assert dest.read_bytes() == BODY


def test_a_server_that_ignores_range_restarts_cleanly(client, server, tmp_path) -> None:
    """Appending a 200 response to a partial file would corrupt it. The
    partial must be discarded instead.
    """
    server.range_mode = "ignore"
    dest = tmp_path / "audio.flac"
    (tmp_path / "audio.flac.part").write_bytes(b"\x00" * 4000)

    _fetch(client, server, dest)

    assert server.requests == ["bytes=4000-"], "Range should still be attempted"
    assert dest.read_bytes() == BODY, "partial was appended to instead of replaced"


def test_a_complete_part_file_gets_a_416_and_restarts(client, server, tmp_path) -> None:
    dest = tmp_path / "audio.flac"
    (tmp_path / "audio.flac.part").write_bytes(BODY)  # already the full length

    _fetch(client, server, dest)

    assert server.requests == [f"bytes={len(BODY)}-", None]
    assert dest.read_bytes() == BODY


def test_resume_can_be_switched_off(client, server, tmp_path) -> None:
    dest = tmp_path / "audio.flac"
    (tmp_path / "audio.flac.part").write_bytes(BODY[:4000])

    _fetch(client, server, dest, resume=False)

    assert server.requests == [None], "resume=False must not send Range"
    assert dest.read_bytes() == BODY


def test_progress_reports_whole_file_position_when_resuming(
    client, server, tmp_path
) -> None:
    """Content-Length on a 206 is the length of the *slice*. Reporting that
    raw would make the progress bar restart from a fraction mid-download.
    """
    dest = tmp_path / "audio.flac"
    (tmp_path / "audio.flac.part").write_bytes(BODY[:4000])

    seen: list[tuple[int, int]] = []
    _fetch(client, server, dest, progress_cb=lambda d, t: seen.append((d, t)))

    assert seen, "progress callback never fired"
    assert seen[0][0] > 4000, "progress restarted from zero instead of resuming"
    assert seen[-1] == (len(BODY), len(BODY))

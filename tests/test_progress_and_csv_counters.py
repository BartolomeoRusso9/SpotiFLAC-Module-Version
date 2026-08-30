"""Three things a long run has to be able to say about itself.

A CSV import and an extension download are both minutes of work behind a
single line of output, and each of them used to under-report in its own way:

  - matching a CSV said "Matching them…" once and nothing again until it
    finished, so a file whose columns were wrong looked identical to one
    that was working;
  - a JS extension's download reported a *percentage* through a callback
    whose contract is byte counts, so the bar filled to "97B / 100B" for a
    30 MB FLAC and the speed was computed from percent-per-second;
  - the ticket step — a whole authenticated round trip that a stalled
    download is usually stuck in — was invisible unless it raised.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import pytest

from SpotiFLAC.core import csv_source
from SpotiFLAC.extensions.provider import JSExtensionProvider, _TransferProgress

# --- the CSV counter -------------------------------------------------------


class _HalfMatchingClient:
    """Matches even-numbered rows, misses odd ones."""

    async def search_tracks_async(self, query, limit=5):
        index = int(query.split()[-1])
        if index % 2:
            return []

        class _Track:
            id = f"id{index}"
            title = f"Song {index}"
            artists = f"Artist {index}"
            album = ""
            duration_ms = 0
            isrc = ""
            external_url = f"https://open.spotify.com/track/id{index}"

        return [_Track()]


def _document(rows: int):
    text = "title,artist\n" + "".join(f"Song {i},Artist {i}\n" for i in range(rows))
    return csv_source.read_text(text, name="playlist.csv")


def test_matching_reports_rows_done_and_tracks_found() -> None:
    """Two different numbers, and the gap between them is the useful one: a
    file 300 rows in with 40 found has the wrong column mapped, and saying
    only "300/300" would hide that until the run ended.
    """
    document = _document(6)
    seen: list[tuple[int, int, int]] = []

    resolution = asyncio.run(
        csv_source.resolve_rows(
            document.rows,
            document=document,
            client=_HalfMatchingClient(),
            concurrency=1,
            on_progress=lambda done, total, found: seen.append((done, total, found)),
        )
    )

    assert [done for done, _, _ in seen] == [1, 2, 3, 4, 5, 6]
    assert all(total == 6 for _, total, _ in seen)
    # `found` only ever climbs, and never past the rows already done.
    found_counts = [found for _, _, found in seen]
    assert found_counts == sorted(found_counts)
    assert all(found <= done for done, _, found in seen)
    # The last call is the summary the user reads, so it has to be exact.
    assert seen[-1] == (6, 6, len(resolution.resolved))


def test_a_broken_progress_callback_does_not_lose_rows() -> None:
    """The counter is a display. A UI push that fails mid-file must not take
    the row it was reporting on down with it.
    """
    document = _document(6)

    def _explode(done, total, found):
        raise RuntimeError("the bridge went away")

    resolution = asyncio.run(
        csv_source.resolve_rows(
            document.rows,
            document=document,
            client=_HalfMatchingClient(),
            concurrency=1,
            on_progress=_explode,
        )
    )

    assert len(resolution.resolved) + len(resolution.unresolved) == 6


# --- the download progress bar --------------------------------------------


def _adapter(provider, transfer):
    """The adapter built inside download_track_async, in isolation."""

    def adapt(fraction, bytes_received=None, bytes_total=None):
        if provider._progress_cb is None:
            return
        if bytes_received is None or not bytes_total:
            bytes_received = transfer.bytes_on_disk
            bytes_total = transfer.estimate_total(fraction)
            if not bytes_received:
                return
        transfer.note(bytes_received, bytes_total)
        provider._progress_cb(bytes_received, bytes_total or 0)

    return adapt


def test_real_byte_counts_reach_the_progress_callback() -> None:
    """The callback's contract is (current_bytes, total_bytes). The bridge
    has been sending bytesReceived/bytesTotal all along; the adapter took
    only the fraction and passed (percent, 100).
    """
    seen: list[tuple[int, int]] = []
    provider = JSExtensionProvider.__new__(JSExtensionProvider)
    provider._progress_cb = lambda current, total: seen.append((current, total))
    transfer = _TransferProgress()

    _adapter(provider, transfer)(0.5, 15_728_640, 31_457_280)

    assert seen == [(15_728_640, 31_457_280)]
    assert transfer.bridge_reporting


def test_a_fraction_without_a_content_length_is_scaled_from_the_file() -> None:
    transfer = _TransferProgress()
    transfer.bytes_on_disk = 5_000_000
    assert transfer.estimate_total(0.5) == 10_000_000


@pytest.mark.parametrize("fraction", [0.0, 0.001, 0.009])
def test_a_total_is_not_invented_from_a_near_zero_fraction(fraction) -> None:
    """Dividing by a fraction that small turns noise into a wild total, and
    a wrong total draws a bar that fills and then keeps going. Unknown (0)
    renders as indeterminate, which is the truth.
    """
    transfer = _TransferProgress()
    transfer.bytes_on_disk = 5_000_000
    assert transfer.estimate_total(fraction) == 0


def test_the_disk_poller_reports_real_sizes_not_a_timer_curve() -> None:
    """It used to emit `1 - 1/(1 + polls * 0.15)` scaled to 100, which moved
    the bar on a timer whether or not any bytes arrived.
    """

    async def _run(tmp: Path):
        part = tmp / "song.flac.part"
        part.write_bytes(b"x" * 4096)

        seen: list[tuple[int, int]] = []
        provider = JSExtensionProvider.__new__(JSExtensionProvider)
        provider._progress_cb = lambda current, total: seen.append((current, total))
        transfer = _TransferProgress()

        stop = asyncio.Event()
        task = asyncio.create_task(
            provider._poll_file_progress_async(tmp / "song.flac", stop, transfer)
        )
        await asyncio.sleep(0.45)
        part.write_bytes(b"x" * 9000)
        await asyncio.sleep(0.45)
        stop.set()
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
        return seen

    with tempfile.TemporaryDirectory() as directory:
        seen = asyncio.run(_run(Path(directory)))

    assert [current for current, _ in seen] == [4096, 9000]
    assert all(total == 0 for _, total in seen), "an unknown total must stay unknown"


def test_the_poller_yields_once_the_bridge_reports_real_bytes() -> None:
    """Both would otherwise write to the same bar, and the poller's number
    is the worse of the two.
    """

    async def _run(tmp: Path):
        part = tmp / "song.flac.part"
        part.write_bytes(b"x" * 4096)

        seen: list[tuple[int, int]] = []
        provider = JSExtensionProvider.__new__(JSExtensionProvider)
        provider._progress_cb = lambda current, total: seen.append((current, total))
        transfer = _TransferProgress()
        transfer.note(4096, 2_000_000)  # the bridge got there first

        stop = asyncio.Event()
        task = asyncio.create_task(
            provider._poll_file_progress_async(tmp / "song.flac", stop, transfer)
        )
        await asyncio.sleep(0.45)
        stop.set()
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
        return seen, transfer

    with tempfile.TemporaryDirectory() as directory:
        seen, transfer = asyncio.run(_run(Path(directory)))

    assert seen == [], "the poller must not fight the bridge"
    # It still keeps the size up to date for the completion log.
    assert transfer.bytes_on_disk == 4096


def test_the_runtime_hands_the_adapter_its_extra_arguments() -> None:
    """JSRuntime tries the three-argument form first and falls back to the
    fraction on TypeError. The adapter has to be the shape that gets the
    bytes, or the fallback silently reinstates the old behaviour.
    """
    from SpotiFLAC.extensions.runtime import JSRuntime

    runtime = JSRuntime.__new__(JSRuntime)
    seen: list[tuple] = []
    runtime._progress_cbs = {7: lambda *args: seen.append(args)}
    runtime._ready_event = asyncio.Event()

    runtime._dispatch(
        {
            "type": "progress",
            "callId": 7,
            "value": 0.5,
            "bytesReceived": 1024,
            "bytesTotal": 2048,
        }
    )
    assert seen == [(0.5, 1024, 2048)]

    provider = JSExtensionProvider.__new__(JSExtensionProvider)
    got: list[tuple[int, int]] = []
    provider._progress_cb = lambda current, total: got.append((current, total))
    transfer = _TransferProgress()
    runtime._progress_cbs = {7: _adapter(provider, transfer)}
    runtime._dispatch(
        {
            "type": "progress",
            "callId": 7,
            "value": 0.5,
            "bytesReceived": 1024,
            "bytesTotal": 2048,
        }
    )
    assert got == [(1024, 2048)]


# --- the ticket step -------------------------------------------------------


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload or "")
        self.headers = {}
        self.url = "https://example.invalid/tickets"
        self.content = self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Enough SignedSessionClient to drive perform_signed_fetch."""

    namespace = "tid"
    authenticated = True

    def __init__(self, response):
        self._response = response

    async def request(self, method, path, json_body=None, extra_headers=None):
        return self._response


def _signed_fetch(response, caplog, path="/tickets"):
    from SpotiFLAC.core.signed_session_mobile import perform_signed_fetch

    with caplog.at_level(logging.INFO, logger="SpotiFLAC.core.signed_session_mobile"):
        asyncio.run(perform_signed_fetch(_Client(response), "POST", path, {}, {}))
    return caplog.text


def test_a_granted_ticket_says_so(caplog) -> None:
    text = _signed_fetch(_Response(200, {"ticket_id": "abc"}), caplog)
    assert "Ticket requested by the extension" in text
    assert "Ticket returned" in text


def test_a_ticket_that_never_comes_back_says_so(caplog) -> None:
    """The case the user is actually chasing: the request went out, the
    answer was not a ticket, and the download went nowhere.
    """
    text = _signed_fetch(_Response(403, None, "forbidden"), caplog)
    assert "Ticket NOT returned" in text
    assert "403" in text


def test_a_200_without_a_ticket_id_is_still_not_a_ticket(caplog) -> None:
    text = _signed_fetch(_Response(200, {"detail": "quota exceeded"}), caplog)
    assert "Ticket NOT returned" in text


def test_an_ordinary_signed_request_is_not_announced_as_a_ticket(caplog) -> None:
    text = _signed_fetch(_Response(200, {"data": {}}), caplog, path="/dl/tid")
    assert "Ticket" not in text


# --- the onProgress scale --------------------------------------------------
#
# The extensions shipped for this runtime disagree about what they pass to
# onProgress, and nothing in the call says which: tidal-web, deezer, amazon
# and qobuz-web report 0..100, pandora and soundcloud report 0..1. The host
# read every value as a fraction and clamped it to 1, so a percentage
# extension's opening onProgress(5) pinned the bar at 100% for the whole
# download. The extensions are third-party and cannot be changed, so the
# bridge infers the scale instead.

_SCALE_HARNESS = """
const run = (label, id, values) =>
  console.log(label + '|' + values.map(v => normalizeProgress(id, v).toFixed(2)).join(','));
run('percent-early', 1, [5, 10, 45, 90, 94, 100]);
run('percent-rounded', 2, [0, 1, 37, 100]);
run('fraction', 3, [0.1, 0.3, 1.0]);
run('junk', 4, [NaN, -3, 'x', 250]);
"""


def _normalizer_source() -> str:
    """The real function out of the shipped bridge, not a copy of it."""
    source = (
        Path(__file__).resolve().parents[1] / "SpotiFLAC" / "extensions" / "_bridge.js"
    ).read_text()
    start = source.index("const progressScale = new Map()")
    end = source.index("};", source.index("const normalizeProgress")) + 2
    return source[start:end]


@pytest.mark.skipif(
    __import__("shutil").which("node") is None,
    reason="needs Node to run the real bridge code",
)
def test_the_bridge_normalises_both_progress_scales() -> None:
    import subprocess

    result = subprocess.run(
        ["node", "-e", _normalizer_source() + _SCALE_HARNESS],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    got = dict(
        line.split("|", 1) for line in result.stdout.strip().splitlines() if "|" in line
    )

    # A percentage extension: 5 is unambiguous, and every later value is
    # read the same way. This used to come out as 1.00 all the way down.
    assert got["percent-early"] == "0.05,0.10,0.45,0.90,0.94,1.00"

    # qobuz-web rounds its percentage, so it sends a bare 1 early on. That
    # value cannot be placed yet, so it is held just short of complete
    # rather than filling the bar and releasing it.
    assert got["percent-rounded"] == "0.00,0.99,0.37,1.00"

    # A fraction extension never trips the rule, so it is left alone. Its
    # closing 1.0 is the same ambiguous value, held at 0.99; the bar is
    # released by clear_item() when the download returns, not by this.
    assert got["fraction"] == "0.10,0.30,0.99"

    # NaN, negatives and non-numbers must not escape the 0..1 range.
    assert got["junk"] == "0.00,0.00,0.00,1.00"

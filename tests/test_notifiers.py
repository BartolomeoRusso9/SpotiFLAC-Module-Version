"""Tests for core/notifiers.py — outbound run notifications."""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import notifiers
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.core.notifiers import NotifierError, build_target
from SpotiFLAC.core.report import RunReport

WEBHOOK = "https://example.invalid/hook"


def _metadata(title="Song", isrc="ITAAA0000001") -> TrackMetadata:
    return TrackMetadata(
        id="sp1",
        title=title,
        artists="An Artist",
        album="An Album",
        album_artist="An Artist",
        isrc=isrc,
    )


# ── Configuration ─────────────────────────────────────────────────────────


def test_unknown_kind_is_refused():
    with pytest.raises(NotifierError):
        build_target("carrier-pigeon", WEBHOOK)


def test_unknown_event_is_refused():
    with pytest.raises(NotifierError):
        build_target("webhook", WEBHOOK, on="whenever")


def test_a_url_is_required():
    with pytest.raises(NotifierError):
        build_target("webhook", "")


def test_non_http_schemes_are_refused():
    for url in ("file:///etc/passwd", "ftp://example.invalid/x", "gopher://x"):
        with pytest.raises(NotifierError):
            build_target("webhook", url)


def test_url_comes_from_the_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv(notifiers.NOTIFY_URL_ENV, WEBHOOK)
    assert build_target("webhook").url == WEBHOOK


def test_telegram_needs_a_token_and_chat_id(monkeypatch):
    monkeypatch.delenv(notifiers.NOTIFY_TOKEN_ENV, raising=False)
    with pytest.raises(NotifierError):
        build_target("telegram", chat_id="123")
    with pytest.raises(NotifierError):
        build_target("telegram", token="bot-token")

    target = build_target("telegram", token="bot-token", chat_id="123")
    assert target.url == "https://api.telegram.org/botbot-token/sendMessage"


def test_private_hosts_are_allowed():
    """Self-hosted ntfy on a LAN is the main use case, not something to block."""
    target = build_target("ntfy", "http://192.168.1.10:8080/spotiflac")
    assert target.kind == "ntfy"


# ── Event selection ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("on", "success", "expected"),
    [
        ("both", True, True),
        ("both", False, True),
        ("success", True, True),
        ("success", False, False),
        ("failure", True, False),
        ("failure", False, True),
        # "summary" is a per-run decision, so no per-track message fires.
        ("summary", True, False),
        ("summary", False, False),
    ],
)
def test_wants_honours_the_event_filter(on, success, expected):
    assert build_target("webhook", WEBHOOK, on=on).wants(success) is expected


# ── Message shaping ───────────────────────────────────────────────────────


def test_discord_body_is_content_and_is_truncated():
    target = build_target("discord", WEBHOOK)
    url, body, headers = notifiers.build_request(target, "x" * 5000, {})
    assert url == WEBHOOK
    assert len(body["content"]) <= 1900
    assert headers == {}


def test_ntfy_carries_a_bearer_token():
    target = build_target("ntfy", WEBHOOK, token="tk")
    _url, body, headers = notifiers.build_request(target, "hello", {})
    assert headers["Authorization"] == "Bearer tk"
    assert body["message"] == "hello"


def test_plain_webhook_sends_the_structured_payload():
    target = build_target("webhook", WEBHOOK)
    _url, body, _headers = notifiers.build_request(
        target, "ignored", {"event": "track", "title": "Song"}
    )
    assert body == {"event": "track", "title": "Song"}


def test_track_summary_reads_success_and_failure():
    ok = notifiers.track_summary(
        DownloadResult.ok("ext:tidal-web", "/tmp/x.flac"), _metadata()
    )
    assert "An Artist" in ok and "ext:tidal-web" in ok and "✅" in ok

    bad = notifiers.track_summary(
        DownloadResult.fail("ext:qobuz", "not found"), _metadata()
    )
    assert "not found" in bad and "❌" in bad


# ── Sending ───────────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send(self, target, text, payload) -> bool:
        self.sent.append((target, text, payload))
        return True


def test_hook_sends_per_track_and_omits_the_file_path(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(notifiers, "send_async", recorder.send)

    hook = notifiers.notify_hook(build_target("webhook", WEBHOOK))
    asyncio.run(
        hook(DownloadResult.ok("ext:tidal-web", "/home/me/Music/x.flac"), _metadata())
    )

    assert len(recorder.sent) == 1
    _target, _text, payload = recorder.sent[0]
    assert payload["title"] == "Song"
    assert payload["provider"] == "ext:tidal-web"
    assert "file_path" not in payload
    assert "/home/me" not in str(payload)


def test_hook_stays_silent_when_the_filter_excludes_the_event(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(notifiers, "send_async", recorder.send)

    hook = notifiers.notify_hook(build_target("webhook", WEBHOOK, on="failure"))
    asyncio.run(hook(DownloadResult.ok("ext:tidal-web", "/tmp/x.flac"), _metadata()))

    assert recorder.sent == []


def test_send_async_never_raises_when_the_endpoint_is_down(monkeypatch):
    class Boom:
        def __init__(self, *_a, **_k) -> None:
            pass

        async def post(self, *_a, **_k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("SpotiFLAC.core.http.AsyncHttpClient", Boom)
    target = build_target("webhook", WEBHOOK)
    assert asyncio.run(notifiers.send_async(target, "hi", {})) is False


def test_run_summary_counts_from_the_report_and_lists_failures(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(notifiers, "send_async", recorder.send)

    report = RunReport()
    report(DownloadResult.ok("ext:tidal-web", "/tmp/a.flac"), _metadata("Good"))
    report(DownloadResult.fail("ext:qobuz", "region locked"), _metadata("Bad", "X2"))

    asyncio.run(notifiers.notify_run_summary(build_target("webhook", WEBHOOK), report))

    _target, text, payload = recorder.sent[0]
    assert payload["downloaded"] == 1
    assert payload["failed"] == 1
    assert "1 downloaded, 1 failed" in text
    # Failures are named; successes are not listed.
    assert "Bad" in text and "region locked" in text
    assert "Good" not in text
    # And no path escapes, even via the per-track list.
    assert all("file_path" not in t for t in payload["tracks"])
    assert "/tmp/a.flac" not in str(payload)

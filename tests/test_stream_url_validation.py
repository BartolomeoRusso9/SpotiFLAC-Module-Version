"""stream_to_file() rejects anything that is not an absolute HTTP(S) URL.

A Tidal build shipped a base64 DASH manifest where a CDN link belonged. httpx
accepted it as a *relative* URL and only failed deep inside urllib, raising
`ValueError: unknown url type: '/PD94bWwg…'` with the whole 4 KB manifest in
the message — no provider name, no hint of what went wrong. These tests pin
the early, legible failure that replaced it.
"""

from __future__ import annotations

import base64

import pytest

from SpotiFLAC.core.errors import NetworkError
from SpotiFLAC.core.http import AsyncHttpClient
from SpotiFLAC.core.loop_runner import run_sync

pytest.importorskip("aiofiles")

# The head of the manifest from the original bug report.
MANIFEST_B64 = base64.b64encode(
    b"<?xml version='1.0' encoding='UTF-8'?><MPD "
    b'xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"/>',
).decode()


@pytest.fixture
def client():
    return AsyncHttpClient("tidal", timeout_s=10)


def test_a_base64_manifest_is_refused_before_httpx_sees_it(client, tmp_path) -> None:
    dest = tmp_path / "audio.flac"

    with pytest.raises(NetworkError) as excinfo:
        run_sync(client.stream_to_file(MANIFEST_B64, str(dest)))

    assert "not an absolute HTTP(S) URL" in str(excinfo.value)
    assert not dest.exists()
    assert not dest.with_suffix(".flac.part").exists()


def test_the_error_names_the_provider_and_truncates_the_value(client, tmp_path) -> None:
    dest = tmp_path / "audio.flac"

    with pytest.raises(NetworkError) as excinfo:
        run_sync(client.stream_to_file(MANIFEST_B64, str(dest)))

    message = str(excinfo.value)
    assert "tidal" in message, "the provider must be identifiable from the error"
    # The whole point: the manifest must not be dumped into the log.
    assert MANIFEST_B64 not in message
    assert "…" in message, "a long value should be elided"


@pytest.mark.parametrize(
    "bad_url",
    ["", "ftp://host/x.flac", "//host/x.flac", "/mediatracks/0.mp4", "not a url"],
)
def test_other_non_http_values_are_refused_too(client, tmp_path, bad_url) -> None:
    with pytest.raises(NetworkError):
        run_sync(client.stream_to_file(bad_url, str(tmp_path / "a.flac")))

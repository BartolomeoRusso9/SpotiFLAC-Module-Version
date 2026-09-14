"""Finding a track's Spotify Canvas.

The canvas endpoint has no published schema — the field numbers below were
read off live responses, exactly as core/spotify_protobuf.py's were — so
what these tests pin is that the parser does not *depend* on them: a
renumbered field must still yield the canvas, because the day it changes
nobody will be watching.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import canvas as cv

TRACK_ID = "3OHfY25tqY28d16oZczHc8"
CANVAS_URL = "https://canvaz.scdn.co/upload/artist/abc/video/deadbeef.cnvs.mp4"
AVATAR_URL = "https://i.scdn.co/image/artist-avatar.jpg"


# --- A protobuf encoder of the test's own, so the parser is not checked
# --- against the same code that produced the bytes.


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _canvaz_response(*, url: str = CANVAS_URL, avatar: str = AVATAR_URL) -> bytes:
    """One EntityCanvazResponse carrying a single canvas."""
    artist = _field(1, b"spotify:artist:xyz") + _field(3, avatar.encode())
    entry = (
        _field(1, b"canvas-id")
        + _field(2, url.encode())
        + _field(5, f"spotify:track:{TRACK_ID}".encode())
        + _field(6, artist)
    )
    return _field(1, entry)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """The miss cache is on disk; keep it off the developer's."""
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "cache"))
    import importlib

    from SpotiFLAC.core import response_cache

    importlib.reload(response_cache)
    monkeypatch.setattr(cv, "get_cached_response", response_cache.get)
    monkeypatch.setattr(cv, "put_cached_response", response_cache.put)


# ── The request ──────────────────────────────────────────────────────────


def test_the_request_asks_for_the_track_by_uri() -> None:
    from SpotiFLAC.core.spotify_protobuf import read_fields

    outer = read_fields(cv.encode_canvaz_request(TRACK_ID))
    entity = read_fields(bytes(outer[1][0][1]))

    assert bytes(entity[1][0][1]).decode() == f"spotify:track:{TRACK_ID}"


# ── The response ─────────────────────────────────────────────────────────


def test_the_canvas_url_is_read_out_of_the_response() -> None:
    found = cv.parse_canvaz_response(_canvaz_response())

    assert found is not None
    assert found.url == CANVAS_URL
    assert found.kind == "video"
    assert found.provider == "spotify"


def test_the_artist_avatar_is_not_mistaken_for_the_canvas() -> None:
    """Both are URLs in the same message, and the avatar is the one that
    would ruin the sidecar silently — a .jpg that is not the canvas.
    """
    reordered = _field(
        1, _field(1, _field(3, AVATAR_URL.encode())) + _field(2, CANVAS_URL.encode())
    )

    found = cv.parse_canvaz_response(reordered)

    assert found is not None
    assert found.url == CANVAS_URL


def test_a_renumbered_field_still_yields_the_canvas() -> None:
    """The whole point of scanning rather than indexing field 2."""
    moved = _field(7, _field(9, CANVAS_URL.encode()))

    found = cv.parse_canvaz_response(moved)

    assert found is not None
    assert found.url == CANVAS_URL


def test_an_empty_response_means_the_track_has_no_canvas() -> None:
    """The common case, and an empty 200 is how it arrives."""
    assert cv.parse_canvaz_response(b"") is None


def test_a_truncated_response_does_not_raise() -> None:
    assert cv.parse_canvaz_response(_canvaz_response()[:12]) is None


# ── The JSON wrapper ─────────────────────────────────────────────────────


def test_the_wrapper_payload_is_read_by_key_not_by_position() -> None:
    payload = {
        "canvasesList": [
            {
                "artist": {"avatarUrl": AVATAR_URL},
                "canvasUrl": CANVAS_URL,
                "trackUri": f"spotify:track:{TRACK_ID}",
            },
        ],
    }

    assert cv._url_in_json(payload) == CANVAS_URL


def test_a_wrapper_payload_with_no_canvas_yields_nothing() -> None:
    assert cv._url_in_json({"ok": False, "message": "no canvas found"}) == ""


# ── Suffixes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CANVAS_URL, ".mp4"),
        ("https://canvaz.scdn.co/upload/x/image/y.jpg", ".jpg"),
        ("https://canvaz.scdn.co/upload/x/video/y.mp4?token=abc", ".mp4"),
        ("https://example.test/no-extension-at-all", ".mp4"),
    ],
)
def test_the_sidecar_extension_comes_off_the_url(url: str, expected: str) -> None:
    assert (
        cv.Canvas(url=url, kind=cv._kind_for(url), provider="spotify").suffix
        == expected
    )


# ── Provider order ───────────────────────────────────────────────────────


def _stub(result, calls: list[str], name: str):
    async def _fetch(track_id: str, timeout: int):
        calls.append(name)
        if isinstance(result, Exception):
            raise result
        return result

    return _fetch


def test_the_second_provider_is_asked_when_the_first_has_nothing() -> None:
    calls: list[str] = []
    found = cv.Canvas(url=CANVAS_URL, kind="video", provider="paxsenix")
    providers = {
        "spotify": _stub(None, calls, "spotify"),
        "paxsenix": _stub(found, calls, "paxsenix"),
    }

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv, "_PROVIDERS", providers)
        got = asyncio.run(cv.fetch_canvas_async(TRACK_ID))

    assert got is found
    assert calls == ["spotify", "paxsenix"]


def test_a_provider_that_raises_does_not_stop_the_next_one() -> None:
    calls: list[str] = []
    found = cv.Canvas(url=CANVAS_URL, kind="video", provider="paxsenix")
    providers = {
        "spotify": _stub(RuntimeError("spclient is having a day"), calls, "spotify"),
        "paxsenix": _stub(found, calls, "paxsenix"),
    }

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv, "_PROVIDERS", providers)
        assert asyncio.run(cv.fetch_canvas_async(TRACK_ID)) is found

    assert calls == ["spotify", "paxsenix"]


def test_a_track_with_no_canvas_is_not_asked_about_twice() -> None:
    """Most of the catalogue has none; re-running an album must not pay a
    request per track for the privilege of hearing that again.
    """
    calls: list[str] = []
    providers = {"spotify": _stub(None, calls, "spotify")}

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv, "_PROVIDERS", providers)
        assert (
            asyncio.run(cv.fetch_canvas_async(TRACK_ID, providers=["spotify"])) is None
        )
        assert (
            asyncio.run(cv.fetch_canvas_async(TRACK_ID, providers=["spotify"])) is None
        )

    assert calls == ["spotify"]


def test_an_id_that_is_not_a_spotify_id_is_never_looked_up() -> None:
    """CSV rows and local files carry a filename or a UUID in `id`."""
    calls: list[str] = []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv, "_PROVIDERS", {"spotify": _stub(None, calls, "spotify")})
        assert asyncio.run(cv.fetch_canvas_async("/home/me/track.flac")) is None
        assert asyncio.run(cv.fetch_canvas_async("")) is None

    assert calls == []


# ── A failure is not an absence ───────────────────────────────────────────


def test_a_provider_that_could_not_answer_is_not_remembered_as_no_canvas() -> None:
    """The miss cache lasts a week. A timeout, a 429 or a 503 says nothing
    about whether the track has a canvas, so caching one of those would
    switch the feature off for seven days over one bad minute.
    """
    calls: list[str] = []
    providers = {
        "spotify": _stub(cv.CanvasUnavailable("HTTP 503"), calls, "spotify"),
    }

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv, "_PROVIDERS", providers)
        assert (
            asyncio.run(cv.fetch_canvas_async(TRACK_ID, providers=["spotify"])) is None
        )
        assert (
            asyncio.run(cv.fetch_canvas_async(TRACK_ID, providers=["spotify"])) is None
        )

    assert calls == ["spotify", "spotify"]


def test_one_provider_failing_does_not_cache_the_others_absence() -> None:
    """Both have to have been asked and answered for "no canvas" to mean
    anything about the track.
    """
    calls: list[str] = []
    providers = {
        "spotify": _stub(cv.CanvasUnavailable("timed out"), calls, "spotify"),
        "paxsenix": _stub(None, calls, "paxsenix"),
    }

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv, "_PROVIDERS", providers)
        assert asyncio.run(cv.fetch_canvas_async(TRACK_ID)) is None
        assert asyncio.run(cv.fetch_canvas_async(TRACK_ID)) is None

    assert calls == ["spotify", "paxsenix", "spotify", "paxsenix"]


# ── Which URLs are fetched at all ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        (CANVAS_URL, True),
        ("https://canvaz-cdn.spotifycdn.com/upload/x/video/y.cnvs.mp4", True),
        (AVATAR_URL, True),
        # Plain http, even to the right host: the URL is attacker-chosen.
        ("http://canvaz.scdn.co/upload/x/video/y.mp4", False),
        # The address an SSRF is usually pointed at.
        ("https://169.254.169.254/latest/meta-data/iam/", False),
        ("http://127.0.0.1:8080/admin", False),
        # userinfo before the @ — the real host is evil.test.
        ("https://canvaz.scdn.co@evil.test/y.mp4", False),
        # A lookalike registrable domain, not a subdomain of one.
        ("https://evil-scdn.co/y.mp4", False),
        ("https://scdn.co.evil.test/y.mp4", False),
        ("file:///etc/passwd", False),
        ("", False),
    ],
)
def test_only_the_canvas_cdn_is_ever_requested(url: str, allowed: bool) -> None:
    """Both providers hand back a URL found by *scanning* their response —
    any http string in the protobuf, any canvas-ish key in the JSON — so
    whoever answers either endpoint would otherwise choose what this
    process connects to.
    """
    assert cv.is_canvas_url(url) is allowed


def test_a_rejected_url_is_never_even_requested() -> None:
    """Rejected, not requested-then-discarded: for a link-local address or
    an intranet host, making the request is itself the damage.
    """
    requested: list[str] = []

    class _Client:
        def stream(self, method, url, **kwargs):  # pragma: no cover - must not run
            requested.append(url)
            raise AssertionError("a rejected URL must not reach the network")

    async def _client():
        return _Client()

    hostile = cv.Canvas(
        url="https://169.254.169.254/latest/meta-data/",
        kind="video",
        provider="paxsenix",
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv.NetworkManager, "get_async_client_safe", _client)
        assert asyncio.run(cv.download_canvas_async(hostile)) is None

    assert requested == []


# ── What a 200 is allowed to contain ──────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "is_media"),
    [
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8, True),  # mp4/m4v/mov
        (b"\x1aE\xdf\xa3" + b"\x00" * 10, True),  # webm
        (b"\xff\xd8\xff\xe0" + b"\x00" * 10, True),  # jpeg
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, True),
        (b"GIF89a" + b"\x00" * 8, True),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", True),
        (b'{"error": "not found", "status": 404}', False),
        (b"<!DOCTYPE html><html><head><title>502 Bad Gateway", False),
        (b"", False),
        (b"short", False),
    ],
)
def test_only_real_media_counts_as_a_canvas(payload: bytes, is_media: bool) -> None:
    """A 200 is not proof of media, and whatever comes back is written to
    disk as a sidecar that the "already there" check then trusts forever.
    """
    assert cv.looks_like_canvas_media(payload) is is_media


def test_a_200_that_is_not_media_is_discarded_rather_than_returned() -> None:
    """An error page, a JSON body or an HTML interstitial all arrive as a
    200. The caller writes what comes back to disk under a media
    extension, so this is the only place it can be stopped.
    """

    class _Response:
        status_code = 200

        async def aiter_bytes(self):
            yield b'{"ok": false, "message": "no canvas found"}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def stream(self, method, url, **kwargs):
            assert kwargs["follow_redirects"] is False
            return _Response()

    async def _client():
        return _Client()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv.NetworkManager, "get_async_client_safe", _client)
        canvas = cv.Canvas(url=CANVAS_URL, kind="video", provider="spotify")
        assert asyncio.run(cv.download_canvas_async(canvas)) is None


def test_real_media_on_the_canvas_cdn_comes_back() -> None:
    """The other side of the check above: the happy path still works."""
    payload = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64

    class _Response:
        status_code = 200

        async def aiter_bytes(self):
            yield payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def stream(self, method, url, **kwargs):
            return _Response()

    async def _client():
        return _Client()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cv.NetworkManager, "get_async_client_safe", _client)
        canvas = cv.Canvas(url=CANVAS_URL, kind="video", provider="spotify")
        assert asyncio.run(cv.download_canvas_async(canvas)) == payload

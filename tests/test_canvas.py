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

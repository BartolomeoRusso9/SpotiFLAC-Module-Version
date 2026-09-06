"""The cover art in the TUI: the drawing, the caching, and the seam.

Three things can break here and only one of them is the picture. The seam is
that `cover_url` survives the trip from a `TrackMetadata` through
`DownloadManager` into the dict the Queue panel reads — a field dropped
anywhere along that route leaves an artwork-shaped hole with nothing to
explain it. The cache is the other: a queue broadcasts several times a
second, and a fetch per broadcast would hammer the CDN for an image that is
already on screen.

The drawing itself is checked for geometry and for colour, not for likeness:
a cover N cells wide must come out N/2 rows tall, and the segments must
carry both halves of every cell.
"""

from __future__ import annotations

import asyncio
import functools
import io

import pytest

from SpotiFLAC.core.progress import DownloadManager
from SpotiFLAC.tui import cover_art
from SpotiFLAC.tui.queue_view import QueuePanel


def drives_the_ui(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def _png(size: int = 64, colour: tuple[int, int, int] = (200, 30, 40)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _transparent_png(size: int = 64) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), (0, 0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path, monkeypatch):
    """A cache directory per test, and no memory carried between them."""
    monkeypatch.setenv("SPOTIFLAC_CACHE_DIR", str(tmp_path / "cache"))
    # NO_COLOR / TERM=dumb would switch covers off entirely, and CI sets
    # both often enough that the tests below would silently stop testing.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("SPOTIFLAC_PLAIN_TUI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    cover_art._MEMORY_CACHE.clear()
    yield
    cover_art._MEMORY_CACHE.clear()


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def test_a_square_cover_comes_out_square():
    """N cells wide is N/2 rows tall — the shape of a terminal cell."""
    rows = cover_art.render_cover(_png(), 24)

    assert len(rows) == cover_art.cell_height_for(24) == 12
    assert all(len(row) == 24 for row in rows)


def test_every_cell_carries_two_pixels():
    """Both halves coloured, or the image is half the resolution it claims."""
    rows = cover_art.render_cover(_png(colour=(200, 30, 40)), 8)

    for segment in rows[0]:
        assert segment.text == "▀"
        assert segment.style is not None
        assert segment.style.color is not None
        assert segment.style.bgcolor is not None
        assert segment.style.color.triplet == (200, 30, 40)
        assert segment.style.bgcolor.triplet == (200, 30, 40)


def test_transparency_lands_on_the_matte_not_on_black():
    """A fully transparent PNG must not render as a black square."""
    rows = cover_art.render_cover(_transparent_png(), 6, matte=(30, 30, 46))

    assert rows[0][0].style.color.triplet == (30, 30, 46)


def test_a_zero_width_cover_draws_nothing():
    assert cover_art.render_cover(_png(), 0) == []


def test_undecodable_data_raises_rather_than_drawing_noise():
    with pytest.raises(Exception):
        cover_art.render_cover(b"this is not an image", 12)


def test_covers_are_off_where_there_is_no_colour(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert cover_art.covers_supported() is False


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content


class _CountingClient:
    """Counts requests, because the point of the cache is that there is one."""

    calls = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def get(self, url, **kwargs):
        type(self).calls += 1
        return _FakeResponse(_png())


@pytest.fixture
def counting_client(monkeypatch):
    import SpotiFLAC.core.http as http_module

    _CountingClient.calls = 0
    monkeypatch.setattr(http_module, "AsyncHttpClient", _CountingClient)
    return _CountingClient


@drives_the_ui
async def test_no_url_means_no_request():
    assert await cover_art.cover_bytes("") is None
    assert await cover_art.cover_bytes("   ") is None


def test_the_same_cover_is_fetched_once(counting_client):
    """Twice from memory, and once more with memory cleared: still one GET."""

    async def _run():
        first = await cover_art.cover_bytes("https://example.invalid/a.jpg")
        second = await cover_art.cover_bytes("https://example.invalid/a.jpg")
        assert first == second
        # Memory gone, disk still there — the queue redrawing after a resize
        # must not go back to the network.
        cover_art._MEMORY_CACHE.clear()
        third = await cover_art.cover_bytes("https://example.invalid/a.jpg")
        assert third == first

    asyncio.run(_run())
    assert counting_client.calls == 1


def test_a_failed_fetch_is_none_not_an_exception(monkeypatch):
    import SpotiFLAC.core.http as http_module

    class _Broken:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get(self, url, **kwargs):
            raise OSError("no route to host")

    monkeypatch.setattr(http_module, "AsyncHttpClient", _Broken)
    assert asyncio.run(cover_art.cover_bytes("https://example.invalid/x.jpg")) is None


def test_an_implausibly_large_cover_is_refused(monkeypatch):
    import SpotiFLAC.core.http as http_module

    class _Huge:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get(self, url, **kwargs):
            return _FakeResponse(b"\x00" * (cover_art._MAX_BYTES + 1))

    monkeypatch.setattr(http_module, "AsyncHttpClient", _Huge)
    assert asyncio.run(cover_art.cover_bytes("https://example.invalid/big")) is None


def test_the_disk_cache_does_not_grow_without_bound(monkeypatch):
    from SpotiFLAC.core.paths import cache_path

    monkeypatch.setattr(cover_art, "_DISK_CACHE_MAX", 3)
    directory = cache_path("tui-covers")
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(10):
        (directory / f"{index}.img").write_bytes(b"x")

    cover_art._trim_disk_cache()

    assert len(list(directory.iterdir())) == 3


# ---------------------------------------------------------------------------
# The seam: a cover URL from the track metadata to the queue's stats dict
# ---------------------------------------------------------------------------


@drives_the_ui
async def test_the_cover_url_reaches_the_stats_dict():
    manager = DownloadManager()
    await manager.reset()
    await manager.add_to_queue(
        "t1",
        "So What",
        "Miles Davis",
        "Kind of Blue",
        "spotify:1",
        "https://example.invalid/kind-of-blue.jpg",
    )

    stats = await manager.get_stats()

    assert stats["downloads"][0]["cover_url"] == (
        "https://example.invalid/kind-of-blue.jpg"
    )
    await manager.reset()


@drives_the_ui
async def test_a_caller_that_names_no_cover_still_works():
    """The argument is optional: the GUI and the tests predate it."""
    manager = DownloadManager()
    await manager.reset()
    await manager.add_to_queue("t1", "So What", "Miles Davis", "Kind of Blue", "s1")

    stats = await manager.get_stats()

    assert stats["downloads"][0]["cover_url"] == ""
    await manager.reset()


def test_the_downloader_passes_the_track_cover(monkeypatch):
    """The one call site — a cover dropped here is a cover never seen."""
    import SpotiFLAC.downloader as downloader_module

    seen: list[tuple] = []

    class _Manager:
        async def reset(self):
            return None

        async def add_to_queue(self, *args):
            seen.append(args)

    monkeypatch.setattr(downloader_module, "DownloadManager", lambda: _Manager())

    class _Track:
        id = "t1"
        title = "So What"
        artists = "Miles Davis"
        album = "Kind of Blue"
        external_url = ""
        cover_url = "https://example.invalid/cover.jpg"

        def model_copy(self, update):
            return self

    downloader = downloader_module.SpotiflacDownloader.__new__(
        downloader_module.SpotiflacDownloader
    )
    asyncio.run(downloader._register_queue_async([_Track()]))

    assert seen and seen[0][-1] == "https://example.invalid/cover.jpg"


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


def _item(**overrides) -> dict:
    item = {
        "id": "t1",
        "track_name": "So What",
        "artist_name": "Miles Davis",
        "status": "queued",
        "end_time": 0.0,
        "cover_url": "https://example.invalid/a.jpg",
    }
    item.update(overrides)
    return item


def test_the_header_follows_the_track_being_fetched():
    items = [
        _item(id="t1", status="completed", end_time=10.0, cover_url="a"),
        _item(id="t2", status="downloading", cover_url="b"),
        _item(id="t3", status="queued", cover_url="c"),
    ]

    assert QueuePanel._current_item(items)["id"] == "t2"


def test_when_nothing_is_running_the_header_holds_the_last_one_done():
    items = [
        _item(id="t1", status="completed", end_time=10.0),
        _item(id="t2", status="completed", end_time=42.0),
    ]

    assert QueuePanel._current_item(items)["id"] == "t2"


def test_before_anything_starts_the_header_shows_the_first_in_the_queue():
    items = [_item(id="t1"), _item(id="t2")]

    assert QueuePanel._current_item(items)["id"] == "t1"


def test_an_empty_queue_has_no_current_track():
    assert QueuePanel._current_item([]) is None


# ---------------------------------------------------------------------------
# Which way the terminal can draw
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forget_the_probe():
    """The probe caches for the life of the process; tests must not share it."""
    cover_art.reset_probe_for_tests()
    yield
    cover_art.reset_probe_for_tests()


def _fake_textual_image(monkeypatch, *, picked: str):
    """A stand-in for the extra, having picked one of its four renderers."""
    import sys
    import types

    class _Sixel:
        pass

    class _TGP:
        pass

    class _Halfcell:
        pass

    class _Widget:
        pass

    chosen = {"sixel": _Sixel, "tgp": _TGP, "halfcell": _Halfcell}[picked]

    renderable = types.ModuleType("textual_image.renderable")
    renderable.Image = chosen
    sixel = types.ModuleType("textual_image.renderable.sixel")
    sixel.Image = _Sixel
    tgp = types.ModuleType("textual_image.renderable.tgp")
    tgp.Image = _TGP
    widget = types.ModuleType("textual_image.widget")
    widget.Image = _Widget

    monkeypatch.setitem(sys.modules, "textual_image.renderable", renderable)
    monkeypatch.setitem(sys.modules, "textual_image.renderable.sixel", sixel)
    monkeypatch.setitem(sys.modules, "textual_image.renderable.tgp", tgp)
    monkeypatch.setitem(sys.modules, "textual_image.widget", widget)
    return _Widget


def test_without_the_extra_the_cover_is_drawn_in_cells(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_textual_image(name, *args, **kwargs):
        if name.startswith("textual_image"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_textual_image)

    assert cover_art.probe_image_support() == "halfcell"


def test_sixel_is_used_where_the_terminal_speaks_it(monkeypatch):
    expected = _fake_textual_image(monkeypatch, picked="sixel")

    assert cover_art.probe_image_support() == "sixel"
    assert cover_art._IMAGE_WIDGET is expected


def test_the_kitty_protocol_is_used_where_the_terminal_speaks_it(monkeypatch):
    _fake_textual_image(monkeypatch, picked="tgp")

    assert cover_art.probe_image_support() == "tgp"


def test_the_extras_own_fallback_is_not_taken_over_ours(monkeypatch):
    """It fell through to half cells — the technique we already have.

    Ours shares the download cache and the sizing with the rest of the
    panel, so there is nothing to gain by routing through theirs.
    """
    _fake_textual_image(monkeypatch, picked="halfcell")

    assert cover_art.probe_image_support() == "halfcell"
    assert cover_art._IMAGE_WIDGET is None


def test_the_terminal_is_asked_only_once(monkeypatch):
    _fake_textual_image(monkeypatch, picked="sixel")
    assert cover_art.probe_image_support() == "sixel"

    # A second probe must not re-query: by now Textual owns stdin, and the
    # question would answer "no" and downgrade a working terminal.
    import builtins

    real_import = builtins.__import__

    def _explode(name, *args, **kwargs):
        if name.startswith("textual_image"):
            raise AssertionError("probed twice")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _explode)
    assert cover_art.probe_image_support() == "sixel"


def test_a_colourless_terminal_gets_no_cover_at_all(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    assert cover_art.probe_image_support() == "halfcell"
    assert cover_art._IMAGE_WIDGET is None


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


def test_the_cover_never_outgrows_the_panel():
    cover = cover_art.CoverArt()

    cover.set_cell_height(400)
    assert cover.styles.height.value == cover_art.MAX_ROWS

    cover.set_cell_height(1)
    assert cover.styles.height.value == cover_art.MIN_ROWS


def test_the_cover_stays_square_at_every_size():
    cover = cover_art.CoverArt()

    for requested in range(cover_art.MIN_ROWS, cover_art.MAX_ROWS + 1):
        cover.set_cell_height(requested)
        assert cover.styles.width.value == cover.styles.height.value * 2

"""cover_art.py — The artwork of whatever is downloading, in the terminal.

There are two ways to put a picture in a terminal, and which one you get is
decided by the terminal, not by this file.

**Real pixels.** A few terminals accept image data over an escape sequence:
Sixel (iTerm2, WezTerm) and the Kitty graphics protocol (kitty, Ghostty).
Where one of those is available the cover is the cover — full resolution,
indistinguishable from an image viewer. That path is `textual-image`, an
optional extra (`pip install "SpotiFLAC[images]"`), because it is only worth
installing on a terminal that can use it.

**Coloured cells.** Everywhere else — Apple Terminal implements none of the
protocols above — the image is drawn with `▀`, which fills the top half of a
cell. The foreground colour paints the upper pixel and the background colour
the lower one, so one cell carries two pixels and a square cover stays
square at N cells wide by N/2 tall (:func:`cell_height_for`). This needs
nothing that is not already installed: Pillow writes the embedded artwork
into the downloaded files, and Rich arrives with Textual.

Rich is also what keeps the fallback honest on a 256-colour terminal: it
knows what it is talking to and quantises the palette itself, rather than
emitting 24-bit sequences a terminal will mangle.

The probe is the one piece of this with a hard ordering constraint.
Detecting Sixel or Kitty support means writing a query to the terminal and
reading its reply, and once Textual starts it owns stdin — its input thread
takes the answer and the query hangs or lies. So :func:`probe_image_support`
runs before the app does, from `app.run_tui_async`, and everything after it
reads a cached answer.

Two failures are expected rather than exceptional, and both end the same
way — the widget hides and the panel looks exactly as it did before covers
existed:

* the provider named no artwork, or named one that 404s;
* the terminal has no colour at all (`NO_COLOR`, `TERM=dumb`), where an
  image made of colour would be a rectangle of grey mush.

Covered by tests/test_tui_cover_art.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
from collections import OrderedDict
from pathlib import Path

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.containers import Container
from textual.strip import Strip
from textual.widget import Widget

from ..core.paths import cache_path
from .branding import plain_terminal

logger = logging.getLogger(__name__)

#: Top half of the cell is the foreground, bottom half is the background.
_HALF_BLOCK = "▀"

#: What a transparent pixel is composited onto. Covers are nearly always
#: opaque JPEGs, but a label logo arrives as a PNG often enough, and without
#: this its alpha is discarded and the transparent parts turn solid black.
#: Catppuccin Mocha's "base" — the theme the TUI opens on.
_MATTE = (30, 30, 46)

#: Cells wide when nothing constrains it. The panel overrides this from its
#: own height (see `set_cell_height`); this is the starting point and what a
#: caller that does not care gets.
DEFAULT_WIDTH = 24

#: Rows the cover may occupy, floor and ceiling. Below six it is a smudge;
#: above twenty-two it has eaten the queue it is supposed to be captioning.
MIN_ROWS = 6
MAX_ROWS = 22

#: Refuse anything larger than this many bytes. A cover is a few hundred KB;
#: something claiming to be one and weighing 40 MB is either a mistake or a
#: provider handing back the wrong URL, and decoding it would stall the UI.
_MAX_BYTES = 12 * 1024 * 1024

#: Downloaded covers, keyed by URL. Small — a run touches one album's art,
#: or one per track on a playlist — and it is the difference between a cover
#: that redraws instantly as the queue advances and one that re-reads the
#: disk every time the panel is resized.
_MEMORY_CACHE: OrderedDict[str, bytes] = OrderedDict()
_MEMORY_CACHE_MAX = 24

#: How many files the on-disk cache keeps. Beyond this the oldest go, which
#: matters because this directory would otherwise grow for the life of the
#: install with no one ever looking at it.
_DISK_CACHE_MAX = 200


def cell_height_for(width: int) -> int:
    """Rows a square cover occupies when it is *width* cells wide."""
    return max(1, round(width / 2))


def covers_supported() -> bool:
    """Whether drawing one is worth doing at all here."""
    if plain_terminal():
        return False
    try:
        import PIL  # noqa: F401
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Which of the two ways this terminal can do
# ---------------------------------------------------------------------------

#: "sixel", "tgp" or "halfcell". Filled by `probe_image_support()`.
_BACKEND: str | None = None
#: The `textual-image` widget class, when there is a protocol to use it with.
_IMAGE_WIDGET: type[Widget] | None = None


def probe_image_support() -> str:
    """Asks the terminal whether it can carry real pixels, once.

    **Call this before Textual starts.** The detection writes an escape
    sequence to stdout and reads the reply off stdin, and Textual's input
    thread will otherwise swallow that reply — `textual-image` says so in
    its own docstring. Calling it late does not crash; it silently answers
    "no protocol" and the cover falls back to half cells, which is the kind
    of bug that only shows up on someone else's terminal.

    Returns the backend name, and caches it for every later caller.
    """
    global _BACKEND, _IMAGE_WIDGET
    if _BACKEND is not None:
        return _BACKEND

    _BACKEND = "halfcell"
    if not covers_supported():
        return _BACKEND

    try:
        # Importing this is the probe: the package runs its Sixel and Kitty
        # queries at import time and picks a class from the answers.
        from textual_image.renderable import Image as AutoRenderable
        from textual_image.renderable.sixel import Image as SixelRenderable
        from textual_image.renderable.tgp import Image as TGPRenderable
    except Exception:
        # The extra is not installed, which is the ordinary case and not
        # worth a warning: half cells are a complete answer, just a coarser
        # one.
        logger.debug("textual-image not installed; using half-cell covers")
        return _BACKEND

    if AutoRenderable is SixelRenderable:
        chosen = "sixel"
    elif AutoRenderable is TGPRenderable:
        chosen = "tgp"
    else:
        # It fell through to its own half-cell or unicode renderer, which is
        # the technique below. Use ours: same picture, and it shares the
        # cache and the sizing with everything else here.
        return _BACKEND

    try:
        from textual_image.widget import Image as ImageWidget
    except Exception as exc:
        logger.debug("textual-image widget unavailable: %s", exc)
        return _BACKEND

    _IMAGE_WIDGET = ImageWidget
    _BACKEND = chosen
    return _BACKEND


def image_backend() -> str:
    """The backend chosen earlier, without probing if nobody has."""
    return _BACKEND or "halfcell"


def reset_probe_for_tests() -> None:
    """Forgets the probe. Only the test-suite has any business calling it."""
    global _BACKEND, _IMAGE_WIDGET
    _BACKEND = None
    _IMAGE_WIDGET = None


# ---------------------------------------------------------------------------
# Drawing, the way that works everywhere
# ---------------------------------------------------------------------------


def render_cover(
    data: bytes,
    width: int,
    height: int | None = None,
    matte: tuple[int, int, int] = _MATTE,
) -> list[list[Segment]]:
    """The image in *data* as one list of segments per row.

    Raises whatever Pillow raises. The caller decides what an undecodable
    cover means; swallowing it here would make a corrupt download and an
    album with no artwork look like the same thing, and only one of those is
    worth a line in the log.
    """
    from PIL import Image

    if width < 1:
        return []
    height = height or cell_height_for(width)

    image = Image.open(io.BytesIO(data))
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (*matte, 255))
        image = Image.alpha_composite(background, image)
    image = image.convert("RGB")

    # One cell is one pixel across and two down.
    image = image.resize((width, height * 2), Image.LANCZOS)
    pixels = image.load()

    rows: list[list[Segment]] = []
    for row in range(height):
        top_y, bottom_y = row * 2, row * 2 + 1
        line: list[Segment] = []
        for column in range(width):
            top = pixels[column, top_y]
            bottom = pixels[column, bottom_y]
            line.append(
                Segment(
                    _HALF_BLOCK,
                    Style(
                        color=Color.from_rgb(*top),
                        bgcolor=Color.from_rgb(*bottom),
                    ),
                ),
            )
        rows.append(line)
    return rows


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _cache_file(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:32]
    return cache_path("tui-covers", f"{digest}.img")


def _trim_disk_cache() -> None:
    """Keeps the newest `_DISK_CACHE_MAX` files and drops the rest."""
    directory = cache_path("tui-covers")
    with contextlib.suppress(Exception):
        files = sorted(
            (p for p in directory.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[_DISK_CACHE_MAX:]:
            with contextlib.suppress(Exception):
                stale.unlink()


def _remember(url: str, data: bytes) -> None:
    _MEMORY_CACHE[url] = data
    _MEMORY_CACHE.move_to_end(url)
    while len(_MEMORY_CACHE) > _MEMORY_CACHE_MAX:
        _MEMORY_CACHE.popitem(last=False)


async def cover_bytes(url: str) -> bytes | None:
    """The image behind *url*: from memory, then disk, then the network.

    Returns `None` rather than raising. Every caller is a widget deciding
    whether it has something to draw, and there is no version of "the
    artwork did not arrive" that should interrupt a download.
    """
    url = (url or "").strip()
    if not url:
        return None

    cached = _MEMORY_CACHE.get(url)
    if cached is not None:
        _MEMORY_CACHE.move_to_end(url)
        return cached

    path = _cache_file(url)
    with contextlib.suppress(Exception):
        if path.is_file():
            data = await asyncio.to_thread(path.read_bytes)
            if data:
                _remember(url, data)
                return data

    try:
        from ..core.http import AsyncHttpClient

        client = AsyncHttpClient("cover-art", timeout_s=15)
        response = await client.get(url, follow_redirects=True)
        data = response.content
    except Exception as exc:
        logger.debug("Cover art unavailable for %s: %s", url, exc)
        return None

    if not data or len(data) > _MAX_BYTES:
        return None

    _remember(url, data)
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        await asyncio.to_thread(_trim_disk_cache)
    return data


# ---------------------------------------------------------------------------
# The widgets
# ---------------------------------------------------------------------------


class HalfBlockCover(Widget):
    """The fallback: an image made of two-tone cells."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: list[list[Segment]] = []

    def set_rows(self, rows: list[list[Segment]]) -> None:
        self._rows = rows
        self.refresh()

    def render_line(self, y: int) -> Strip:
        if y >= len(self._rows):
            return Strip.blank(self.size.width)
        return Strip(self._rows[y], len(self._rows[y]))


class CoverArt(Container):
    """A cover that follows whatever URL it is pointed at.

    Hidden until it has an image, and hidden again the moment it does not:
    a placeholder box where the artwork should be is a permanent reminder of
    a missing feature, whereas a panel that simply looks like it always did
    is not a failure at all.

    Which child does the drawing is decided once, by the probe, and never
    changes for the life of the process — the terminal is not going to grow
    Sixel support halfway through a download.
    """

    DEFAULT_CSS = """
    CoverArt {
        height: auto;
    }
    CoverArt > * {
        width: 100%;
        height: 100%;
    }
    """

    def __init__(self, width: int = DEFAULT_WIDTH, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cover_width = max(4, width)
        self._url = ""
        self._data: bytes | None = None
        self._half: HalfBlockCover | None = None
        self._image: Widget | None = None
        self.display = False
        self._apply_size()

    def _apply_size(self) -> None:
        self.styles.width = self._cover_width
        self.styles.height = cell_height_for(self._cover_width)

    def compose(self):
        if _IMAGE_WIDGET is not None:
            self._image = _IMAGE_WIDGET()
            yield self._image
        else:
            self._half = HalfBlockCover()
            yield self._half

    # ------------------------------------------------------------------
    # Size
    # ------------------------------------------------------------------

    def set_cell_height(self, rows: int) -> None:
        """Resizes the cover to *rows* tall, redrawing if it changed.

        The panel drives this from its own height rather than the widget
        picking a number: a cover sized for a full-screen terminal fills a
        short one entirely, and the queue underneath is the thing you were
        looking at.
        """
        rows = max(MIN_ROWS, min(MAX_ROWS, rows))
        width = rows * 2
        if width == self._cover_width:
            return
        self._cover_width = width
        self._apply_size()
        if self._data is not None:
            self._draw(self._data)

    # ------------------------------------------------------------------
    # Source
    # ------------------------------------------------------------------

    def set_source(self, url: str) -> None:
        """Points the widget at a cover; a no-op if it is already there.

        The guard is the whole reason this is cheap to call: the queue
        broadcasts stats several times a second, and every one of them would
        otherwise start another fetch of the artwork already on screen.
        """
        url = (url or "").strip()
        if url == self._url:
            return
        self._url = url
        if not url or not covers_supported():
            self.clear()
            return
        self.run_worker(self._load(url), exclusive=True, group="cover")

    def clear(self) -> None:
        self._data = None
        self.display = False

    async def _load(self, url: str) -> None:
        data = await cover_bytes(url)
        # The queue may have moved on while this was in flight; drawing the
        # previous track's artwork over the current one is worse than a beat
        # of delay.
        if url != self._url:
            return
        if data is None:
            self.clear()
            return
        self._data = data
        self._draw(data)

    def _draw(self, data: bytes) -> None:
        if self._image is not None:
            try:
                from PIL import Image

                self._image.image = Image.open(io.BytesIO(data))
            except Exception as exc:
                logger.debug("Cover art could not be decoded: %s", exc)
                self.clear()
                return
            self.display = True
            return

        if self._half is None:
            return
        try:
            rows = render_cover(data, self._cover_width)
        except Exception as exc:
            logger.debug("Cover art could not be decoded: %s", exc)
            self.clear()
            return
        self._half.set_rows(rows)
        self.display = True

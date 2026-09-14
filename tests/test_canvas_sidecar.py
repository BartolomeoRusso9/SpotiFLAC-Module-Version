"""Saving the Spotify Canvas beside the download.

The canvas is never embedded — FLAC has no video stream to hold one — so
the sidecar *is* the feature, and it follows the .lrc layout for the same
reason: one player pairs a file with the audio by filename, another wants
everything collected as "Artist - Title".

Nothing here reaches the network: what is under test is when a canvas is
asked for, where it lands, and that a track without one (most of them)
costs nothing and leaves nothing behind.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from SpotiFLAC import downloader as dl
from SpotiFLAC.core.canvas import Canvas
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

PAYLOAD = b"\x00\x00\x00\x18ftypmp42 not really a video"
CANVAS = Canvas(
    url="https://canvaz.scdn.co/upload/artist/abc/video/deadbeef.cnvs.mp4",
    kind="video",
    provider="spotify",
)


def _track() -> TrackMetadata:
    return TrackMetadata(
        id="3OHfY25tqY28d16oZczHc8",
        title="RATATA",
        artists="Capo Plaza",
        artist_names=["Capo Plaza"],
        album="20 (Deluxe Edition)",
        album_artist="Capo Plaza",
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "RATATA - Capo Plaza.flac"
    path.write_bytes(b"not really audio")
    return path


@pytest.fixture
def spotify(monkeypatch):
    """A canvas that exists, and a count of what was asked for."""
    state = {"canvas": CANVAS, "fetches": 0, "downloads": 0}

    async def _fetch(track_id, *, providers=None, timeout=7):
        state["fetches"] += 1
        state["track_id"] = track_id
        state["providers"] = providers
        return state["canvas"]

    async def _download(canvas, *, timeout=20):
        state["downloads"] += 1
        return PAYLOAD

    monkeypatch.setattr("SpotiFLAC.core.canvas.fetch_canvas_async", _fetch)
    monkeypatch.setattr("SpotiFLAC.core.canvas.download_canvas_async", _download)
    return state


def _run(audio: Path, **opts_kwargs) -> None:
    opts = dl.DownloadOptions(output_dir=str(audio.parent), **opts_kwargs)
    asyncio.run(
        dl._write_canvas_sidecars_async(
            DownloadResult.ok("tidal", str(audio)),
            _track(),
            opts,
        ),
    )


def test_the_sidecar_takes_the_audio_files_name(audio, spotify) -> None:
    _run(audio, save_canvas=True)

    sidecar = audio.with_suffix(".mp4")
    assert sidecar.read_bytes() == PAYLOAD


def test_the_library_copy_is_artist_first(audio, spotify, tmp_path) -> None:
    library = tmp_path / "Canvas"
    _run(audio, canvas_library_dir=str(library))

    assert (library / "Capo Plaza - RATATA.mp4").read_bytes() == PAYLOAD
    assert not audio.with_suffix(".mp4").exists()


def test_both_layouts_share_one_download(audio, spotify, tmp_path) -> None:
    """The CDN URL is signed and short-lived; fetching it twice is a second
    chance to fail for no benefit.
    """
    library = tmp_path / "Canvas"
    _run(audio, save_canvas=True, canvas_library_dir=str(library))

    assert audio.with_suffix(".mp4").exists()
    assert (library / "Capo Plaza - RATATA.mp4").exists()
    assert spotify["downloads"] == 1


def test_an_image_canvas_keeps_its_own_extension(audio, spotify) -> None:
    spotify["canvas"] = Canvas(
        url="https://canvaz.scdn.co/upload/artist/abc/image/x.jpg",
        kind="image",
        provider="spotify",
    )
    _run(audio, save_canvas=True)

    assert audio.with_suffix(".jpg").exists()
    assert not audio.with_suffix(".mp4").exists()


def test_nothing_is_fetched_when_neither_destination_is_asked_for(
    audio, spotify
) -> None:
    _run(audio)

    assert spotify["fetches"] == 0
    assert not audio.with_suffix(".mp4").exists()


def test_a_track_without_a_canvas_leaves_no_file(audio, spotify) -> None:
    """Most of the catalogue. Not an error, and not an empty sidecar."""
    spotify["canvas"] = None
    _run(audio, save_canvas=True)

    assert not audio.with_suffix(".mp4").exists()
    assert spotify["downloads"] == 0


def test_a_canvas_already_on_disk_is_not_fetched_again(audio, spotify) -> None:
    """What makes re-running a half-finished album cheap."""
    audio.with_suffix(".mp4").write_bytes(b"already here")

    _run(audio, save_canvas=True)

    assert spotify["fetches"] == 0
    assert audio.with_suffix(".mp4").read_bytes() == b"already here"


def test_the_missing_half_is_still_filled_in(audio, spotify, tmp_path) -> None:
    """Beside the track already, but the library copy never made it."""
    audio.with_suffix(".mp4").write_bytes(b"already here")
    library = tmp_path / "Canvas"

    _run(audio, save_canvas=True, canvas_library_dir=str(library))

    assert (library / "Capo Plaza - RATATA.mp4").read_bytes() == PAYLOAD
    assert audio.with_suffix(".mp4").read_bytes() == b"already here"


def test_the_configured_provider_order_is_the_one_used(audio, spotify) -> None:
    _run(audio, save_canvas=True, canvas_providers=["paxsenix"])

    assert spotify["providers"] == ["paxsenix"]


def test_no_partial_file_is_left_behind(audio, spotify) -> None:
    _run(audio, save_canvas=True)

    assert not list(audio.parent.glob("*.part"))


def test_an_unwritable_destination_does_not_fail_the_download(
    audio, spotify, tmp_path
) -> None:
    """The track is downloaded and tagged by this point. A canvas that
    cannot be written is worth a log line, not a failed track.
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory")

    _run(audio, canvas_library_dir=str(blocked))  # must not raise


def test_a_download_that_returns_nothing_writes_nothing(audio, spotify, monkeypatch):
    async def _empty(canvas, *, timeout=20):
        return None

    monkeypatch.setattr("SpotiFLAC.core.canvas.download_canvas_async", _empty)
    _run(audio, save_canvas=True)

    assert not audio.with_suffix(".mp4").exists()


def test_an_existing_image_sidecar_is_not_re_fetched(audio, spotify) -> None:
    """The cheap pre-check runs before the canvas is known, so it has to
    recognise a sidecar in any of the extensions one can take. Knowing
    only about .mp4, it missed an image canvas and went back to the
    network for that track on every single run — which matters now that
    the pre-check also guards every skipped track of a re-run.
    """
    audio.with_suffix(".jpg").write_bytes(PAYLOAD)

    _run(audio, save_canvas=True)

    assert spotify["fetches"] == 0
    assert spotify["downloads"] == 0


def test_a_skipped_track_still_gets_its_canvas(audio, spotify) -> None:
    """Switching --save-canvas on over an already-downloaded library has
    to collect the canvases: every track skips, and a skip is the only
    state those tracks will ever be in again.
    """
    opts = dl.DownloadOptions(output_dir=str(audio.parent), save_canvas=True)
    asyncio.run(
        dl._write_canvas_sidecars_async(
            DownloadResult.skipped_result("tidal", str(audio), fmt="flac"),
            _track(),
            opts,
        ),
    )

    assert audio.with_suffix(".mp4").read_bytes() == PAYLOAD

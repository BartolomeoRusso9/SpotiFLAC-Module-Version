"""Tests for the post-download MP3 transcoding option."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from SpotiFLAC.core.base import BaseProvider
from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.core.transcode import (
    normalize_bitrate,
    normalize_transcode_format,
    transcode_file_async,
    transcoded_file_exists,
)
from SpotiFLAC.downloader import (
    DownloadOptions,
    DownloadWorker,
    download_one_async,
    transcode_target_path,
)

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def _metadata() -> TrackMetadata:
    return TrackMetadata(
        id="track-1",
        title="Song",
        artists="Artist One, Artist Two",
        album="Album",
        album_artist="Artist One",
    )


class RecordingProvider:
    """Provider stub that records whether it was asked to download."""

    name = "dummy"

    def __init__(self, result: DownloadResult | None = None) -> None:
        self.calls = 0
        self._result = result

    def set_progress_callback(self, cb) -> None:
        pass

    async def download_track_async(self, metadata, output_dir, **kwargs):
        self.calls += 1
        return self._result or DownloadResult.fail(self.name, "no result configured")


# ─── Option normalization ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("none", None),
        ("mp3", "mp3"),
        (".MP3", "mp3"),
        ("mp3_320", "mp3"),
    ],
)
def test_normalize_transcode_format(value, expected) -> None:
    assert normalize_transcode_format(value) == expected


def test_normalize_transcode_format_rejects_unsupported() -> None:
    with pytest.raises(ValueError, match="Unsupported transcode format"):
        normalize_transcode_format("opus")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "320k"), ("320", "320k"), ("320k", "320k"), ("192 kbps", "192k")],
)
def test_normalize_bitrate(value, expected) -> None:
    assert normalize_bitrate(value) == expected


def test_options_normalize_on_construction() -> None:
    opts = DownloadOptions(
        output_dir="/tmp", transcode_to="MP3_320", transcode_bitrate=256
    )
    assert opts.transcode_to == "mp3"
    assert opts.transcode_bitrate == "256k"


def test_options_reject_unsupported_format() -> None:
    with pytest.raises(ValueError, match="Unsupported transcode format"):
        DownloadOptions(output_dir="/tmp", transcode_to="flac")


# ─── Target path ─────────────────────────────────────────────────────────────


class _PathProbe(BaseProvider):
    """Only used for its inherited `_build_output_path`."""

    name = "path-probe"

    async def download_track_async(self, metadata, output_dir, **kwargs):
        raise NotImplementedError


def test_transcode_target_matches_provider_naming(tmp_path) -> None:
    """The skip lookup must use the exact name a provider would produce."""
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    meta = _metadata()

    target = transcode_target_path(meta, str(tmp_path), opts, position=2)
    # __new__ skips the HTTP client setup: path building needs no I/O
    probe = object.__new__(_PathProbe)
    provider_path = probe._build_output_path(
        meta,
        str(tmp_path),
        filename_format=opts.filename_format,
        position=2,
        include_track_num=opts.use_track_numbers,
        use_album_track_num=opts.use_album_track_numbers,
        first_artist_only=opts.first_artist_only,
    )

    assert target == provider_path.with_suffix(".mp3")


def test_transcode_target_is_none_when_disabled(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path))
    assert transcode_target_path(_metadata(), str(tmp_path), opts) is None


def test_transcode_target_follows_output_path(tmp_path) -> None:
    exact = tmp_path / "custom name.flac"
    opts = DownloadOptions(
        output_dir=str(tmp_path),
        transcode_to="mp3",
        output_path=str(exact),
    )
    assert transcode_target_path(_metadata(), str(tmp_path), opts) == exact.with_suffix(
        ".mp3",
    )


# ─── Skipping already-downloaded tracks ──────────────────────────────────────


def test_existing_mp3_is_skipped_without_calling_providers(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    meta = _metadata()
    target = transcode_target_path(meta, str(tmp_path), opts)
    target.write_bytes(b"already here")

    provider = RecordingProvider()
    result = asyncio.run(download_one_async(meta, str(tmp_path), [provider], opts))

    assert result.success
    assert result.skipped
    assert result.file_path == str(target)
    assert result.format == "mp3"
    assert provider.calls == 0


def test_empty_mp3_is_not_treated_as_downloaded(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    meta = _metadata()
    target = transcode_target_path(meta, str(tmp_path), opts)
    target.touch()

    provider = RecordingProvider(DownloadResult.fail("dummy", "nope"))
    asyncio.run(download_one_async(meta, str(tmp_path), [provider], opts))

    assert provider.calls == 1


# ─── Converting finished downloads ───────────────────────────────────────────


def _fake_transcode(tmp_path):
    async def _run(source, *, fmt="mp3", bitrate="320k", keep_original=False):
        dest = Path(source).with_suffix(f".{fmt}")
        dest.write_bytes(b"converted audio")
        if not keep_original:
            Path(source).unlink()
        return dest

    return _run


def test_successful_download_is_transcoded(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    meta = _metadata()
    flac = tmp_path / "Song - Artist One, Artist Two.flac"
    flac.write_bytes(b"lossless audio")

    provider = RecordingProvider(DownloadResult.ok("dummy", str(flac)))
    with patch(
        "SpotiFLAC.downloader.transcode_file_async",
        _fake_transcode(tmp_path),
    ):
        result = asyncio.run(download_one_async(meta, str(tmp_path), [provider], opts))

    assert result.success
    assert not result.skipped
    assert result.format == "mp3"
    assert result.file_path == str(flac.with_suffix(".mp3"))
    assert not flac.exists()


def test_leftover_source_file_is_converted_on_skip(tmp_path) -> None:
    """A track skipped because the FLAC exists still ends up as MP3."""
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    meta = _metadata()
    flac = tmp_path / "Song - Artist One, Artist Two.flac"
    flac.write_bytes(b"lossless audio")

    provider = RecordingProvider(DownloadResult.skipped_result("dummy", str(flac)))
    with patch(
        "SpotiFLAC.downloader.transcode_file_async",
        _fake_transcode(tmp_path),
    ):
        result = asyncio.run(download_one_async(meta, str(tmp_path), [provider], opts))

    assert result.success
    assert not result.skipped
    assert transcoded_file_exists(flac.with_suffix(".mp3"))


def test_provider_mp3_is_left_untouched(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    meta = _metadata()
    mp3 = tmp_path / "already.mp3"
    mp3.write_bytes(b"mp3 audio")

    provider = RecordingProvider(DownloadResult.ok("dummy", str(mp3), "mp3"))

    async def _boom(*_args, **_kwargs):
        raise AssertionError("should not transcode an MP3")

    with patch("SpotiFLAC.downloader.transcode_file_async", _boom):
        result = asyncio.run(download_one_async(meta, str(tmp_path), [provider], opts))

    assert result.success
    assert result.file_path == str(mp3)


def test_transcode_failure_fails_the_track_and_keeps_source(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    meta = _metadata()
    flac = tmp_path / "Song - Artist One, Artist Two.flac"
    flac.write_bytes(b"lossless audio")

    async def _fail(*_args, **_kwargs):
        msg = "ffmpeg exploded"
        raise RuntimeError(msg)

    provider = RecordingProvider(DownloadResult.ok("dummy", str(flac)))
    with patch("SpotiFLAC.downloader.transcode_file_async", _fail):
        result = asyncio.run(download_one_async(meta, str(tmp_path), [provider], opts))

    assert not result.success
    assert "ffmpeg exploded" in (result.error or "")
    assert flac.exists()


def test_worker_stops_early_when_ffmpeg_is_missing(tmp_path) -> None:
    from SpotiFLAC.core.errors import SpotiflacError

    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")

    with patch.object(DownloadWorker, "_build_providers", return_value=[]):
        worker = DownloadWorker(tracks=[], opts=opts)

    with (
        patch(
            "SpotiFLAC.core.ffmpeg_check.check_ffmpeg",
            return_value={"available": False, "error": "not found"},
        ),
        pytest.raises(SpotiflacError, match="requires ffmpeg"),
    ):
        asyncio.run(worker.run_async())


# ─── Real conversion (requires ffmpeg) ───────────────────────────────────────


@requires_ffmpeg
def test_transcode_preserves_tags_cover_and_lyrics(tmp_path) -> None:
    import subprocess

    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3

    source = tmp_path / "Song - Artist.flac"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1:sample_rate=44100",
            "-ac",
            "2",
            "-c:a",
            "flac",
            str(source),
        ],
        check=True,
    )

    cover = b"\xff\xd8\xff" + b"fake jpeg bytes" * 8
    audio = FLAC(str(source))
    audio["TITLE"] = "Song"
    audio["ARTIST"] = ["Artist One", "Artist Two"]
    audio["ALBUM"] = "Album"
    audio["TRACKNUMBER"] = "3"
    audio["TRACKTOTAL"] = "12"
    audio["ISRC"] = "USRC12345678"
    audio["LYRICS"] = "first line\nsecond line"
    picture = Picture()
    picture.data = cover
    picture.type = 3
    picture.mime = "image/jpeg"
    audio.add_picture(picture)
    audio.save()

    dest = asyncio.run(transcode_file_async(source, bitrate="320k"))

    assert dest == source.with_suffix(".mp3")
    assert not source.exists()
    assert transcoded_file_exists(dest)

    tags = ID3(str(dest))
    assert str(tags["TIT2"]) == "Song"
    assert str(tags["TPE1"]) == "Artist One, Artist Two"
    assert str(tags["TRCK"]) == "3/12"
    assert str(tags["TSRC"]) == "USRC12345678"
    assert "second line" in str(tags["USLT::eng"])
    assert tags["APIC:Cover"].data == cover


@requires_ffmpeg
def test_transcode_keeps_original_when_requested(tmp_path) -> None:
    import subprocess

    source = tmp_path / "keep.flac"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1:sample_rate=44100",
            "-ac",
            "2",
            "-c:a",
            "flac",
            str(source),
        ],
        check=True,
    )

    dest = asyncio.run(transcode_file_async(source, keep_original=True))

    assert source.exists()
    assert dest.exists()

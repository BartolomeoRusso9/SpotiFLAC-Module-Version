"""Tests for the multi-playlist sync (--playlist / --m3u)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from SpotiFLAC.core.models import DownloadResult, TrackMetadata, build_filename
from SpotiFLAC.core.playlist_sync import (
    PlaylistSource,
    build_plan,
    dedup_key,
    entry_for,
    find_existing,
    index_audio_files,
    mark_existing,
    playlist_file_name,
    render_m3u,
    track_stem,
    write_if_changed_async,
)
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader
from SpotiFLAC.providers.base import BaseProvider


def _track(
    track_id: str,
    title: str = "Song",
    artists: str = "Artist One",
    isrc: str = "",
    duration_ms: int = 210_000,
) -> TrackMetadata:
    return TrackMetadata(
        id=track_id,
        title=title,
        artists=artists,
        album="Album",
        album_artist=artists.split(",")[0],
        isrc=isrc,
        duration_ms=duration_ms,
    )


def _source(name: str, tracks: list[TrackMetadata], url: str = "") -> PlaylistSource:
    return PlaylistSource(
        url=url or f"https://open.spotify.com/playlist/{name}",
        name=name,
        tracks=tuple(tracks),
    )


# ─── Deduplication ───────────────────────────────────────────────────────────


def test_isrc_identifies_the_same_recording() -> None:
    a = _track("spotify-1", isrc="USUM71703861")
    b = _track("tidal-2", title="Song (Remaster)", isrc="usum71703861")
    assert dedup_key(a) == dedup_key(b)


def test_title_and_artist_used_without_isrc() -> None:
    a = _track("id-1", title="Hello World", artists="Adele")
    b = _track("id-2", title="hello   world!", artists="Adele, Someone Else")
    assert dedup_key(a) == dedup_key(b)


def test_different_songs_stay_apart() -> None:
    assert dedup_key(_track("id-1", title="A")) != dedup_key(_track("id-2", title="B"))


def test_isrc_wins_over_matching_names() -> None:
    """Two different recordings of the same song are two different files."""
    a = _track("id-1", isrc="AAAAA0000001")
    b = _track("id-2", isrc="BBBBB0000002")
    assert dedup_key(a) != dedup_key(b)


# ─── Plan ────────────────────────────────────────────────────────────────────


def test_shared_track_is_planned_once() -> None:
    shared = _track("shared", title="Shared", isrc="USUM71703861")
    plan = build_plan(
        [
            _source("Morning", [shared, _track("a", title="A")]),
            _source("Evening", [_track("b", title="B"), shared]),
        ],
    )

    assert len(plan.tracks) == 3
    assert [p.position for p in plan.tracks] == [1, 2, 3]
    # Both playlists still list it, in their own order.
    assert plan.playlists[0].keys[0] == dedup_key(shared)
    assert plan.playlists[1].keys[1] == dedup_key(shared)


def test_repeats_inside_one_playlist_are_listed_once() -> None:
    dupe = _track("dupe", isrc="USUM71703861")
    plan = build_plan([_source("Mine", [dupe, _track("other", title="Other"), dupe])])
    assert len(plan.playlists[0].keys) == 2
    assert len(plan.tracks) == 2


def test_playlists_sharing_a_name_get_distinct_files() -> None:
    plan = build_plan(
        [
            _source("Chill", [_track("a")], url="https://x/1"),
            _source("Chill", [_track("b", title="B")], url="https://x/2"),
        ],
    )
    names = [p.file_name for p in plan.playlists]
    assert names == ["Chill.m3u8", "Chill (2).m3u8"]


def test_playlist_file_name_is_filesystem_safe() -> None:
    assert playlist_file_name("Rock/Metal: 2024?") == "Rock_Metal_ 2024_.m3u8"
    assert playlist_file_name("Rock", "m3u") == "Rock.m3u"


# ─── Tracks already on disk ──────────────────────────────────────────────────


class _PathProbe(BaseProvider):
    """Only used for its inherited `_build_output_path`."""

    name = "path-probe"

    async def download_track_async(self, metadata, output_dir, **kwargs):
        raise NotImplementedError


def test_track_stem_matches_provider_naming(tmp_path) -> None:
    """The lookup must use the exact name a provider would produce."""
    opts = DownloadOptions(output_dir=str(tmp_path))
    meta = _track("id-1", artists="Artist One, Artist Two")

    probe = object.__new__(_PathProbe)
    provider_path = probe._build_output_path(
        meta,
        str(tmp_path),
        filename_format=opts.filename_format,
        position=3,
        include_track_num=opts.use_track_numbers,
        use_album_track_num=opts.use_album_track_numbers,
        first_artist_only=opts.first_artist_only,
    )

    assert track_stem(meta, opts, 3) == provider_path.stem


def test_existing_file_is_found_whatever_the_extension(tmp_path) -> None:
    (tmp_path / "Song - Artist One.m4a").write_bytes(b"audio")
    index = index_audio_files(tmp_path)

    assert (
        find_existing(index, "Song - Artist One") == tmp_path / "Song - Artist One.m4a"
    )
    assert find_existing(index, "Missing - Nobody") is None


def test_empty_file_does_not_count_as_downloaded(tmp_path) -> None:
    (tmp_path / "Song - Artist One.flac").touch()
    assert find_existing(index_audio_files(tmp_path), "Song - Artist One") is None


def test_lossless_is_preferred_over_lossy(tmp_path) -> None:
    (tmp_path / "Song - Artist One.mp3").write_bytes(b"audio")
    (tmp_path / "Song - Artist One.flac").write_bytes(b"audio")
    found = find_existing(index_audio_files(tmp_path), "Song - Artist One")
    assert found.suffix == ".flac"


def test_transcoding_only_accepts_the_target_format(tmp_path) -> None:
    """A leftover FLAC still has to go through ffmpeg."""
    (tmp_path / "Song - Artist One.flac").write_bytes(b"audio")
    index = index_audio_files(tmp_path)

    assert find_existing(index, "Song - Artist One", transcode_to="mp3") is None

    (tmp_path / "Song - Artist One.mp3").write_bytes(b"audio")
    index = index_audio_files(tmp_path)
    found = find_existing(index, "Song - Artist One", transcode_to="mp3")
    assert found == tmp_path / "Song - Artist One.mp3"


def test_files_in_subfolders_are_found(tmp_path) -> None:
    nested = tmp_path / "Artist One"
    nested.mkdir()
    (nested / "Song - Artist One.flac").write_bytes(b"audio")
    index = index_audio_files(tmp_path)
    assert (
        find_existing(index, "Song - Artist One") == nested / "Song - Artist One.flac"
    )


def test_mark_existing_splits_pending_from_present(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path))
    here = _track("a", title="Here")
    missing = _track("b", title="Missing")
    (tmp_path / f"{track_stem(here, opts, 1)}.flac").write_bytes(b"audio")

    plan = build_plan([_source("Mine", [here, missing])])
    plan = mark_existing(plan, index_audio_files(tmp_path), opts)

    assert [p.track.title for p in plan.present] == ["Here"]
    assert [p.track.title for p in plan.pending] == ["Missing"]


# ─── M3U rendering ───────────────────────────────────────────────────────────


def test_render_m3u_uses_relative_paths(tmp_path) -> None:
    playlist = tmp_path / "Mine.m3u8"
    entries = [
        entry_for(_track("a", title="A", artists="Artist One"), tmp_path / "A.flac"),
        entry_for(
            _track("b", title="B", duration_ms=0),
            tmp_path / "Artist One" / "B.mp3",
        ),
    ]

    assert render_m3u(entries, playlist) == (
        "#EXTM3U\n"
        "#EXTINF:210,Artist One - A\n"
        "A.flac\n"
        "#EXTINF:-1,Artist One - B\n"
        "Artist One/B.mp3\n"
    )


def test_render_m3u_of_an_empty_playlist(tmp_path) -> None:
    assert render_m3u([], tmp_path / "Mine.m3u8") == "#EXTM3U\n"


def test_write_only_touches_the_file_when_content_changed(tmp_path) -> None:
    target = tmp_path / "Mine.m3u8"

    assert asyncio.run(write_if_changed_async(target, "#EXTM3U\n")) is True
    mtime = target.stat().st_mtime_ns

    assert asyncio.run(write_if_changed_async(target, "#EXTM3U\n")) is False
    assert target.stat().st_mtime_ns == mtime

    assert asyncio.run(write_if_changed_async(target, "#EXTM3U\nA.flac\n")) is True
    assert target.read_text(encoding="utf-8") == "#EXTM3U\nA.flac\n"


# ─── End to end ──────────────────────────────────────────────────────────────


class _FakeProvider:
    """Writes a file where a real provider would, and counts the downloads."""

    name = "fake"

    def __init__(self, extension: str = ".flac") -> None:
        self.extension = extension
        self.downloaded: list[str] = []

    def set_progress_callback(self, cb) -> None:
        pass

    def close(self) -> None:
        pass

    async def download_track_async(self, metadata, output_dir, **kwargs):
        name = build_filename(
            metadata,
            fmt=kwargs.get("filename_format", "{title} - {artist}"),
            position=kwargs.get("position", 1),
            include_track_number=kwargs.get("include_track_num", False),
            use_album_track_number=kwargs.get("use_album_track_num", False),
            first_artist_only=kwargs.get("first_artist_only", False),
            extension=self.extension,
        )
        dest = Path(output_dir) / name
        if dest.exists() and dest.stat().st_size > 0:
            return DownloadResult.skipped_result(self.name, str(dest))
        dest.write_bytes(b"audio data")
        self.downloaded.append(metadata.title)
        return DownloadResult.ok(self.name, str(dest), self.extension.lstrip("."))


def _run_sync(opts, sources, provider, m3u_format="m3u8"):
    downloader = SpotiflacDownloader(opts)
    by_url = {s.url: s for s in sources}

    async def fake_metadata(_self, url):
        source = by_url[url]
        return source.name, list(source.tracks), {"type": "playlist"}

    with (
        patch.object(SpotiflacDownloader, "_resolve_metadata_async", fake_metadata),
        patch(
            "SpotiFLAC.downloader._build_providers_for_name",
            return_value=[provider],
        ),
    ):
        asyncio.run(
            downloader.run_playlists_async(
                [s.url for s in sources],
                m3u_format=m3u_format,
            ),
        )


@pytest.fixture
def sources() -> list[PlaylistSource]:
    shared = _track("shared", title="Shared", isrc="USUM71703861")
    return [
        _source("Morning", [shared, _track("a", title="A", isrc="AAAAA0000001")]),
        _source("Evening", [_track("b", title="B", isrc="BBBBB0000002"), shared]),
    ]


def test_shared_track_is_downloaded_once(tmp_path, sources) -> None:
    provider = _FakeProvider()
    _run_sync(DownloadOptions(output_dir=str(tmp_path)), sources, provider)

    assert sorted(provider.downloaded) == ["A", "B", "Shared"]
    assert sorted(p.name for p in tmp_path.glob("*.flac")) == [
        "A - Artist One.flac",
        "B - Artist One.flac",
        "Shared - Artist One.flac",
    ]


def test_one_m3u_per_playlist_listing_its_own_tracks(tmp_path, sources) -> None:
    _run_sync(DownloadOptions(output_dir=str(tmp_path)), sources, _FakeProvider())

    assert (tmp_path / "Morning.m3u8").read_text(encoding="utf-8") == (
        "#EXTM3U\n"
        "#EXTINF:210,Artist One - Shared\n"
        "Shared - Artist One.flac\n"
        "#EXTINF:210,Artist One - A\n"
        "A - Artist One.flac\n"
    )
    assert (tmp_path / "Evening.m3u8").read_text(encoding="utf-8") == (
        "#EXTM3U\n"
        "#EXTINF:210,Artist One - B\n"
        "B - Artist One.flac\n"
        "#EXTINF:210,Artist One - Shared\n"
        "Shared - Artist One.flac\n"
    )


def test_second_run_downloads_nothing_and_keeps_the_files(tmp_path, sources) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path))
    _run_sync(opts, sources, _FakeProvider())
    before = (tmp_path / "Morning.m3u8").stat().st_mtime_ns

    provider = _FakeProvider()
    _run_sync(opts, sources, provider)

    assert provider.downloaded == []
    assert (tmp_path / "Morning.m3u8").stat().st_mtime_ns == before


def test_playlist_file_follows_a_changed_playlist(tmp_path, sources) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path))
    _run_sync(opts, sources, _FakeProvider())

    added = _track("c", title="C", isrc="CCCCC0000003")
    grown = [
        PlaylistSource(
            url=sources[0].url,
            name=sources[0].name,
            tracks=(*sources[0].tracks, added),
        ),
        sources[1],
    ]
    provider = _FakeProvider()
    _run_sync(opts, grown, provider)

    assert provider.downloaded == ["C"]
    assert (
        (tmp_path / "Morning.m3u8")
        .read_text(encoding="utf-8")
        .endswith(
            "#EXTINF:210,Artist One - C\nC - Artist One.flac\n",
        )
    )
    # The untouched playlist keeps listing only its own tracks.
    assert "C - Artist One.flac" not in (tmp_path / "Evening.m3u8").read_text(
        encoding="utf-8",
    )


def test_pre_existing_file_is_never_downloaded(tmp_path, sources) -> None:
    """Whatever format it is already in, when transcoding is off."""
    (tmp_path / "Shared - Artist One.m4a").write_bytes(b"audio data")

    provider = _FakeProvider()
    _run_sync(DownloadOptions(output_dir=str(tmp_path)), sources, provider)

    assert "Shared" not in provider.downloaded
    assert "Shared - Artist One.m4a" in (tmp_path / "Morning.m3u8").read_text(
        encoding="utf-8",
    )


def test_failed_track_is_left_out_of_the_playlist_file(tmp_path, sources) -> None:
    class _FailingProvider(_FakeProvider):
        async def download_track_async(self, metadata, output_dir, **kwargs):
            if metadata.title == "A":
                return DownloadResult.fail(self.name, "nope")
            return await super().download_track_async(metadata, output_dir, **kwargs)

    _run_sync(DownloadOptions(output_dir=str(tmp_path)), sources, _FailingProvider())

    morning = (tmp_path / "Morning.m3u8").read_text(encoding="utf-8")
    assert "Shared - Artist One.flac" in morning
    assert "A - Artist One.flac" not in morning


def test_m3u_none_downloads_without_writing_playlist_files(tmp_path, sources) -> None:
    provider = _FakeProvider()
    _run_sync(
        DownloadOptions(output_dir=str(tmp_path)),
        sources,
        provider,
        m3u_format="none",
    )

    assert sorted(provider.downloaded) == ["A", "B", "Shared"]
    assert list(tmp_path.glob("*.m3u*")) == []


def test_m3u_extension_follows_the_requested_format(tmp_path, sources) -> None:
    _run_sync(
        DownloadOptions(output_dir=str(tmp_path)),
        sources,
        _FakeProvider(),
        m3u_format="m3u",
    )
    assert (tmp_path / "Morning.m3u").is_file()


def test_transcoded_tracks_are_listed_as_mp3(tmp_path, sources) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")

    async def fake_transcode(source, *, fmt="mp3", bitrate="320k", keep_original=False):
        dest = Path(source).with_suffix(f".{fmt}")
        dest.write_bytes(b"converted audio")
        Path(source).unlink()
        return dest

    with (
        patch("SpotiFLAC.downloader.transcode_file_async", fake_transcode),
        patch("SpotiFLAC.downloader.ensure_ffmpeg_available", lambda fmt: None),
    ):
        _run_sync(opts, sources, _FakeProvider())

    assert (tmp_path / "Morning.m3u8").read_text(encoding="utf-8") == (
        "#EXTM3U\n"
        "#EXTINF:210,Artist One - Shared\n"
        "Shared - Artist One.mp3\n"
        "#EXTINF:210,Artist One - A\n"
        "A - Artist One.mp3\n"
    )
    assert list(tmp_path.glob("*.flac")) == []


def test_transcoded_run_skips_tracks_already_in_mp3(tmp_path, sources) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path), transcode_to="mp3")
    (tmp_path / "Shared - Artist One.mp3").write_bytes(b"converted audio")

    provider = _FakeProvider()
    with patch("SpotiFLAC.downloader.ensure_ffmpeg_available", lambda fmt: None):
        _run_sync(opts, sources, provider)

    assert "Shared" not in provider.downloaded
    assert "Shared - Artist One.mp3" in (tmp_path / "Morning.m3u8").read_text(
        encoding="utf-8",
    )

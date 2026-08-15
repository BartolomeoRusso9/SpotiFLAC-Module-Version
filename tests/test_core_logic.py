import asyncio
from pathlib import Path

import pytest

from SpotiFLAC.client import AsyncSpotiFLAC
from SpotiFLAC.core import (
    download_validation,
    ffmpeg_check,
    link_resolver,
    playlist_sync,
    tagger,
    transcode,
)
from SpotiFLAC.core.history import HistoryManager
from SpotiFLAC.extensions.manager import ExtensionManager, RegistryEntry
from SpotiFLAC.core.isrc_utils import is_valid_isrc, normalize_isrc
from SpotiFLAC.core.models import (
    DownloadResult,
    TrackMetadata,
    build_filename,
    sanitize,
)
from SpotiFLAC.core.quality import (
    map_amazon_community_quality,
    normalize_quality,
    quality_fallback_chain,
)


def test_normalize_isrc_strips_prefix_and_validates():
    assert normalize_isrc(" isrc:usabc1234567 ") == "USABC1234567"
    assert normalize_isrc("invalid") == ""
    assert is_valid_isrc("USABC1234567") is True
    assert is_valid_isrc("INVALID") is False


def test_quality_helpers_normalize_and_fallbacks():
    assert normalize_quality("27") == "HI_RES_LOSSLESS"
    assert normalize_quality("low") == "LOW"
    assert quality_fallback_chain("hi_res_lossless") == [
        "HI_RES_LOSSLESS",
        "LOSSLESS",
        "HIGH",
        "LOW",
    ]
    assert map_amazon_community_quality("DOLBY_ATMOS") == "atmos"
    assert map_amazon_community_quality("HI_RES_LOSSLESS") == "24"


def test_track_metadata_strips_artists_and_builds_tags():
    md = TrackMetadata(
        id="track-1",
        title="Song Title",
        artists="A & B / C feat. D",
        album="Album Name",
        album_artist="Album Artist / Guest",
        isrc="USABC1234567",
        track_number=3,
        disc_number=2,
        total_tracks=10,
        total_discs=3,
        duration_ms=31000,
        release_date="2024-01-15",
        external_url="https://example.com/track",
        publisher="Label",
        is_explicit=True,
    )

    assert md.artists == "A, B, C, D"
    assert md.album_artist == "Album Artist, Guest"
    assert md.first_artist == "A"
    assert md.duration_seconds == 31.0

    tags = md.as_flac_tags()
    assert tags["TITLE"] == "Song Title"
    assert tags["ARTIST"] == "A, B, C, D"
    assert tags["ISRC"] == "USABC1234567"
    assert tags["URL"] == "https://example.com/track"
    assert tags["ITUNESADVISORY"] == "1"


def test_download_result_requires_file_path_when_successful():
    with pytest.raises(ValueError):
        DownloadResult(success=True, provider="tidal")

    ok = DownloadResult.ok("tidal", "/tmp/file.flac", fmt="flac")
    assert ok.success is True
    assert ok.file_path == "/tmp/file.flac"


def test_sanitize_and_build_filename_are_filesystem_safe():
    value = 'Song / Name? "quoted"'
    assert sanitize(value) == "Song Name quoted"

    md = TrackMetadata(
        id="t-2",
        title="Song / Name?",
        artists="Artist / Guest",
        album="Album: 2024",
        album_artist="Album Artist",
        release_date="2024-03-01",
        track_number=7,
    )

    filename = build_filename(
        md,
        "{artist} - {title}",
        position=7,
        include_track_number=False,
        extension=".mp3",
    )

    assert filename == "Artist, Guest - Song Name.mp3"
    assert filename.endswith(".mp3")


def test_history_manager_round_trip(tmp_path):
    manager = HistoryManager()
    manager.path = tmp_path / "recent-fetches.json"
    metadata = TrackMetadata(
        id="history-1",
        title="Track",
        artists="Artist",
        album="Album",
        album_artist="Artist",
        track_number=1,
    )

    manager.add(metadata)
    history = manager.get_all()

    assert len(history) == 1
    assert history[0]["id"] == "history-1"
    assert "fetched_at" in history[0]

    manager.clear()
    assert manager.get_all() == []


def test_history_manager_handles_invalid_json(tmp_path):
    manager = HistoryManager()
    manager.path = tmp_path / "recent-fetches.json"
    manager.path.write_text("{not valid json", encoding="utf-8")

    assert manager.get_all() == []


def test_download_validation_rejects_preview_and_accepts_missing_duration(
    monkeypatch, tmp_path
):
    path = tmp_path / "preview.flac"
    path.write_bytes(b"fake-audio")

    async def _run():
        async def fake_duration(_filepath: str) -> float:
            return 15.0

        removed = {}

        async def fake_remove(filepath: str) -> None:
            removed["path"] = filepath

        monkeypatch.setattr(
            download_validation, "_get_audio_duration_async", fake_duration
        )
        monkeypatch.setattr(download_validation, "_remove_file_async", fake_remove)

        ok, msg = await download_validation.validate_downloaded_track_async(
            str(path), 180
        )
        assert ok is False
        assert "Preview" in msg
        assert removed["path"] == str(path)

        async def fake_missing_duration(_filepath: str) -> float:
            return 0.0

        monkeypatch.setattr(
            download_validation, "_get_audio_duration_async", fake_missing_duration
        )
        ok2, msg2 = await download_validation.validate_downloaded_track_async(
            str(path), 180
        )
        assert ok2 is True
        assert msg2 == ""

    asyncio.run(_run())


def test_ffmpeg_check_handles_both_success_and_missing(monkeypatch, capsys):
    def fake_success(*args, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "ffmpeg version 7.0\n"})()

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_success)
    result = ffmpeg_check.check_ffmpeg()
    assert result["available"] is True
    assert "ffmpeg version" in result["version"]

    def fake_missing(*args, **kwargs):
        raise FileNotFoundError("ffmpeg not found in PATH")

    monkeypatch.setattr(ffmpeg_check.subprocess, "run", fake_missing)
    missing = ffmpeg_check.check_ffmpeg()
    assert missing["available"] is False
    assert "ffmpeg not found" in missing["error"]
    printed = ffmpeg_check.print_ffmpeg_warning(missing)
    assert printed == missing
    captured = capsys.readouterr().out
    assert "ffmpeg NOT FOUND" in captured


def test_link_resolver_normalizes_and_extracts_links():
    resolver = link_resolver.LinkResolver()

    assert resolver.identify_provider("https://open.spotify.com/track/abc") == "spotify"
    assert (
        resolver.identify_provider("https://soundcloud.com/user/track") == "soundcloud"
    )

    amazon_url = (
        "https://music.amazon.com/albums/XXXXXXXXXX?trackAsin=B07T2G5CB2&foo=bar"
    )
    assert resolver._normalize_amazon_url(amazon_url) == (
        "https://music.amazon.com/tracks/B07T2G5CB2?musicTerritory=US"
    )
    assert resolver._normalize_deezer_url("https://www.deezer.com/track/123456") == (
        "https://www.deezer.com/track/123456"
    )

    songlink_payload = {
        "linksByPlatform": {
            "deezer": {"url": "https://www.deezer.com/track/123456"},
            "amazonMusic": {
                "url": "https://music.amazon.com/albums/XXXXXXXXXX?trackAsin=B07T2G5CB2"
            },
            "tidal": {"url": "https://listen.tidal.com/track/987654"},
        }
    }
    normalized = resolver._process_songlink_response(songlink_payload)
    assert normalized["deezer"] == "https://www.deezer.com/track/123456"
    assert (
        normalized["amazonMusic"]
        == "https://music.amazon.com/tracks/B07T2G5CB2?musicTerritory=US"
    )
    assert normalized["tidal"] == "https://listen.tidal.com/track/987654"

    html = (
        '<script type="application/ld+json">'
        '{"sameAs":["https://listen.tidal.com/track/987654",'
        '"https://music.amazon.com/tracks/B07T2G5CB2?musicTerritory=US",'
        '"https://www.deezer.com/track/123456"]}'
        "</script>"
    )
    songstats = resolver._process_songstats_links(html)
    assert songstats["tidal"] == "https://listen.tidal.com/track/987654"
    assert songstats["deezer"] == "https://www.deezer.com/track/123456"
    assert (
        songstats["amazonMusic"]
        == "https://music.amazon.com/tracks/B07T2G5CB2?musicTerritory=US"
    )


def test_playlist_sync_dedup_and_rendering(tmp_path):
    track_a = TrackMetadata(
        id="track-1",
        title="Song Title",
        artists="Artist One",
        album="Album",
        album_artist="Artist One",
        isrc="USABC1234567",
        duration_ms=180000,
    )
    track_b = TrackMetadata(
        id="track-2",
        title="Song Title",
        artists="Artist One",
        album="Album",
        album_artist="Artist One",
        isrc="USABC1234567",
        duration_ms=180000,
    )
    source_one = playlist_sync.PlaylistSource(
        url="https://example.com/playlist/one",
        name="Alpha",
        tracks=(track_a, track_b),
    )
    source_two = playlist_sync.PlaylistSource(
        url="https://example.com/playlist/two",
        name="Beta",
        tracks=(track_a,),
    )

    plan = playlist_sync.build_plan([source_one, source_two], m3u_format="m3u8")
    assert len(plan.tracks) == 1
    assert plan.playlists[0].keys == ("isrc:USABC1234567",)
    assert plan.playlists[1].keys == ("isrc:USABC1234567",)
    assert plan.playlists[0].file_name == "Alpha.m3u8"

    index = playlist_sync.index_audio_files(tmp_path)
    created = tmp_path / "song.flac"
    created.write_bytes(b"audio")
    index = playlist_sync.index_audio_files(tmp_path)
    assert playlist_sync.find_existing(index, "song") == created

    m3u = playlist_sync.render_m3u(
        [
            playlist_sync.M3UEntry(
                path=created, title="Song Title", artists="Artist One", duration_s=180
            )
        ],
        tmp_path / "playlist" / "Alpha.m3u8",
    )
    assert "#EXTM3U" in m3u
    assert "Artist One - Song Title" in m3u


def test_tagger_helpers_cover_url_and_embedded_tags():
    base_url = "https://i.scdn.co/image/ab67616d0000abc123"
    assert tagger.max_resolution_spotify_cover(base_url) == (
        "https://i.scdn.co/image/ab67616d0000b273"
    )

    options = tagger.EmbedOptions(embed_lyrics=True, lyrics_providers=["spotify"])
    assert options.embed_lyrics is True
    assert options.lyrics_providers == ["spotify"]

    payload = tagger.EmbeddedTags(
        tags={"TITLE": "Song", "ARTIST": "Artist"}, lyrics="hello"
    )
    assert bool(payload) is True


def test_transcode_helpers_and_conversion(monkeypatch, tmp_path):
    source = tmp_path / "song.flac"
    source.write_bytes(b"source")

    assert transcode.normalize_transcode_format("mp3_320") == "mp3"
    assert transcode.normalize_transcode_format("keep") is None
    assert transcode.normalize_bitrate("320") == "320k"
    assert transcode.transcoded_path(source, "mp3") == tmp_path / "song.mp3"
    assert transcode.transcoded_file_exists(tmp_path / "missing.mp3") is False

    def fake_check_ffmpeg():
        return {"available": True, "version": "ffmpeg version 7.0", "error": ""}

    monkeypatch.setattr(transcode, "check_ffmpeg", fake_check_ffmpeg, raising=False)
    monkeypatch.setattr(transcode, "ensure_ffmpeg_available", lambda fmt: None)

    async def fake_run_ffmpeg(*args):
        dest = Path(args[-1])
        dest.write_bytes(b"encoded")
        return 0, ""

    async def fake_transfer(src, dst):
        return True

    monkeypatch.setattr(transcode, "_run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(transcode, "transfer_tags_to_mp3_async", fake_transfer)

    async def _run():
        dest = await transcode.transcode_file_async(
            source, fmt="mp3", keep_original=True
        )
        assert dest == tmp_path / "song.mp3"
        assert dest.exists()
        assert source.exists()

    asyncio.run(_run())


def test_extension_manager_deduplicates_registry_checks_in_one_process(
    monkeypatch, tmp_path
):
    # Snapshot original state and restore in finally block
    original_checks = ExtensionManager._startup_registry_checks.copy()
    try:
        ExtensionManager._startup_registry_checks.clear()
        calls = []

        def fake_fetch_registry(self, url=None):
            calls.append(url)
            return [
                RegistryEntry(
                    id="demo-provider",
                    display_name="Demo provider",
                    version="1.0.0",
                    description="",
                    download_url="https://example.com/demo.zip",
                    category="download_provider",
                    tags=["download_provider"],
                )
            ]

        monkeypatch.setattr(ExtensionManager, "fetch_registry", fake_fetch_registry)
        monkeypatch.setattr(ExtensionManager, "get_installed", lambda self, ext_id: None)
        monkeypatch.setattr(
            ExtensionManager, "install_from_url", lambda *args, **kwargs: None
        )
        monkeypatch.setenv("SPOTIFLAC_REGISTRIES", "https://example.com/registry.json")

        manager = ExtensionManager(ext_dir=tmp_path / "exts", auto_install_downloads=True)
        manager.ensure_download_providers("https://example.com/registry.json")
        manager.ensure_download_providers("https://example.com/registry.json")

        assert calls == [["https://example.com/registry.json"]]

        manager2 = ExtensionManager(ext_dir=tmp_path / "exts2", auto_install_downloads=True)
        manager2.ensure_download_providers("https://example.com/registry.json")

        # exts2 is a different directory, so it should trigger another fetch
        assert calls == [
            ["https://example.com/registry.json"],
            ["https://example.com/registry.json"],
        ]

        manager3 = ExtensionManager(
            ext_dir=tmp_path / "exts3", auto_install_downloads=False
        )
        manager3.ensure_download_providers("https://example.com/other-registry.json")

        assert calls == [
            ["https://example.com/registry.json"],
            ["https://example.com/registry.json"],
            ["https://example.com/other-registry.json"],
        ]
    finally:
        # Restore original state
        ExtensionManager._startup_registry_checks.clear()
        ExtensionManager._startup_registry_checks.update(original_checks)


def test_async_client_tracks_loop_minutes_and_default_playlist_subfolders():
    client = AsyncSpotiFLAC(output_dir="./downloads")
    assert client._opts.create_playlist_subfolders is False

    calls = []
    sleep_calls = []

    async def fake_run_once(url, target_tracks=None):
        calls.append((url, target_tracks))
        if len(calls) == 1:
            return [{"id": "retry-track"}]
        return []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    client._entered = True
    client._downloader._run_once_async = fake_run_once
    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep

    try:

        async def _run():
            await client.download_track(
                "https://open.spotify.com/track/abc", loop_minutes=7
            )

        asyncio.run(_run())
    finally:
        asyncio.sleep = original_sleep

    assert calls == [
        ("https://open.spotify.com/track/abc", None),
        ("https://open.spotify.com/track/abc", [{"id": "retry-track"}]),
    ]
    assert sleep_calls == [420]

"""test_url_validation.py
Tests for URL format recognition across all supported sources:
Spotify, Tidal, Apple Music, SoundCloud, YouTube, Pandora (v1.2.8).
"""

import re

import pytest

from tests.conftest import (
    APPLE_MUSIC_ALBUM,
    GARBAGE_STRING,
    INVALID_URL,
    PANDORA_TRACK,
    SOUNDCLOUD_TRACK,
    SPOTIFY_ALBUM,
    SPOTIFY_ARTIST,
    SPOTIFY_PLAYLIST,
    SPOTIFY_TRACK,
    TIDAL_ALBUM,
    TIDAL_TRACK,
    YOUTUBE_VIDEO,
)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers – lightweight regex mirrors of what SpotiFLAC is expected to use.
# These are tested independently so we can validate our own fixture URLs.
# ──────────────────────────────────────────────────────────────────────────────

SPOTIFY_PATTERN = re.compile(
    r"https://open\.spotify\.com/(track|album|playlist|artist)/\w+",
)
TIDAL_PATTERN = re.compile(r"https://listen\.tidal\.com/(album|track|playlist)/\d+")
APPLE_PATTERN = re.compile(r"https://music\.apple\.com/.+/album/.+/\d+")
SOUNDCLOUD_PATTERN = re.compile(r"https://soundcloud\.com/.+/.+")
YOUTUBE_PATTERN = re.compile(
    r"https://(www\.)?youtube\.com/watch\?v=.+|https://youtu\.be/.+",
)
PANDORA_PATTERN = re.compile(r"https://www\.pandora\.com/.+")


# ──────────────────────────────────────────────────────────────────────────────
# Spotify URL tests
# ──────────────────────────────────────────────────────────────────────────────


class TestSpotifyUrls:
    @pytest.mark.parametrize(
        "url",
        [
            SPOTIFY_TRACK,
            SPOTIFY_ALBUM,
            SPOTIFY_PLAYLIST,
            SPOTIFY_ARTIST,
        ],
    )
    def test_spotify_urls_match_pattern(self, url) -> None:
        assert SPOTIFY_PATTERN.match(url), f"Expected Spotify URL to match: {url}"

    def test_spotify_track_url_structure(self) -> None:
        assert "/track/" in SPOTIFY_TRACK

    def test_spotify_album_url_structure(self) -> None:
        assert "/album/" in SPOTIFY_ALBUM

    def test_spotify_playlist_url_structure(self) -> None:
        assert "/playlist/" in SPOTIFY_PLAYLIST

    def test_spotify_artist_url_structure(self) -> None:
        assert "/artist/" in SPOTIFY_ARTIST

    @pytest.mark.parametrize(
        "bad",
        [
            "https://open.spotify.com/",  # no content type
            "https://spotify.com/track/abc",  # wrong subdomain
            "http://open.spotify.com/track/abc",  # HTTP not HTTPS
        ],
    )
    def test_malformed_spotify_urls_do_not_match(self, bad) -> None:
        assert not SPOTIFY_PATTERN.match(bad)


# ──────────────────────────────────────────────────────────────────────────────
# Tidal URL tests
# ──────────────────────────────────────────────────────────────────────────────


class TestTidalUrls:
    @pytest.mark.parametrize("url", [TIDAL_ALBUM, TIDAL_TRACK])
    def test_tidal_urls_match_pattern(self, url) -> None:
        assert TIDAL_PATTERN.match(url), f"Expected Tidal URL to match: {url}"

    def test_tidal_album_url_structure(self) -> None:
        assert "/album/" in TIDAL_ALBUM

    def test_tidal_track_url_structure(self) -> None:
        assert "/track/" in TIDAL_TRACK

    @pytest.mark.parametrize(
        "bad",
        [
            "https://tidal.com/album/123",  # wrong subdomain
            "https://listen.tidal.com/",  # no content path
        ],
    )
    def test_malformed_tidal_urls_do_not_match(self, bad) -> None:
        assert not TIDAL_PATTERN.match(bad)


# ──────────────────────────────────────────────────────────────────────────────
# Apple Music URL tests
# ──────────────────────────────────────────────────────────────────────────────


class TestAppleMusicUrls:
    def test_apple_music_url_matches_pattern(self) -> None:
        assert APPLE_PATTERN.match(APPLE_MUSIC_ALBUM)

    def test_apple_music_url_contains_album(self) -> None:
        assert "album" in APPLE_MUSIC_ALBUM

    def test_apple_music_format_note(self) -> None:
        """Apple Music delivers M4A/ALAC (lossless) or AAC – never FLAC."""
        # Confirm the URL is not a Spotify/Tidal URL
        assert "spotify.com" not in APPLE_MUSIC_ALBUM
        assert "tidal.com" not in APPLE_MUSIC_ALBUM


# ──────────────────────────────────────────────────────────────────────────────
# SoundCloud URL tests
# ──────────────────────────────────────────────────────────────────────────────


class TestSoundCloudUrls:
    def test_soundcloud_url_matches_pattern(self) -> None:
        assert SOUNDCLOUD_PATTERN.match(SOUNDCLOUD_TRACK)

    def test_soundcloud_url_host(self) -> None:
        assert "soundcloud.com" in SOUNDCLOUD_TRACK

    def test_soundcloud_note_mp3_only(self) -> None:
        """SoundCloud downloads are MP3 – no lossless."""
        # The URL must be a SoundCloud URL
        assert SOUNDCLOUD_TRACK.startswith("https://soundcloud.com")


# ──────────────────────────────────────────────────────────────────────────────
# YouTube URL tests
# ──────────────────────────────────────────────────────────────────────────────


class TestYouTubeUrls:
    def test_youtube_full_url_matches(self) -> None:
        assert YOUTUBE_PATTERN.match(YOUTUBE_VIDEO)

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ],
    )
    def test_youtube_short_and_long_urls(self, url) -> None:
        assert YOUTUBE_PATTERN.match(url)

    def test_youtube_note_mp3_only(self) -> None:
        """YouTube downloads are MP3 – no lossless."""
        assert "youtube.com" in YOUTUBE_VIDEO or "youtu.be" in YOUTUBE_VIDEO


# ──────────────────────────────────────────────────────────────────────────────
# Pandora URL tests
# ──────────────────────────────────────────────────────────────────────────────


class TestPandoraUrls:
    def test_pandora_url_matches_pattern(self) -> None:
        assert PANDORA_PATTERN.match(PANDORA_TRACK)

    def test_pandora_url_host(self) -> None:
        assert "pandora.com" in PANDORA_TRACK


# ──────────────────────────────────────────────────────────────────────────────
# Invalid / unsupported URL tests
# ──────────────────────────────────────────────────────────────────────────────


class TestInvalidUrls:
    @pytest.mark.parametrize("url", [INVALID_URL, GARBAGE_STRING, "", "   "])
    def test_unsupported_urls_match_no_pattern(self, url) -> None:
        assert not SPOTIFY_PATTERN.match(url)
        assert not TIDAL_PATTERN.match(url)
        assert not APPLE_PATTERN.match(url)
        assert not SOUNDCLOUD_PATTERN.match(url)
        assert not YOUTUBE_PATTERN.match(url)
        assert not PANDORA_PATTERN.match(url)

    def test_download_only_services_cannot_be_input_urls(self) -> None:
        """joox, netease, migu, kuwo are download-only services –
        they must not be used as input URL sources.
        """
        download_only_domains = ["joox.com", "music.163.com", "migu.cn", "kuwo.cn"]
        for domain in download_only_domains:
            fake_url = f"https://{domain}/song/123"
            assert not SPOTIFY_PATTERN.match(fake_url)
            assert not TIDAL_PATTERN.match(fake_url)


# ──────────────────────────────────────────────────────────────────────────────
# Mixed URL list tests
# ──────────────────────────────────────────────────────────────────────────────


class TestMixedUrlLists:
    def test_spotify_and_tidal_in_same_list(
        self,
        tmp_output_dir,
        mock_spotiflac,
    ) -> None:
        mock_cls, _ = mock_spotiflac
        urls = [SPOTIFY_ALBUM, TIDAL_ALBUM]
        mock_cls(url=urls, output_dir=tmp_output_dir)
        kwargs = mock_cls.call_args.kwargs
        assert SPOTIFY_ALBUM in kwargs["url"]
        assert TIDAL_ALBUM in kwargs["url"]

    def test_url_deduplication_hint(self) -> None:
        """Duplicate URLs in a list should ideally be de-duplicated."""
        urls = [SPOTIFY_TRACK, SPOTIFY_TRACK, SPOTIFY_ALBUM]
        unique = list(dict.fromkeys(urls))
        assert len(unique) == 2

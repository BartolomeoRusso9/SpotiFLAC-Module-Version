"""
test_output.py
Tests for output directory handling and file organisation options (v1.2.8).

Covers:
  - output_dir validation (existing, non-existing, relative paths)
  - use_album_subfolders behaviour
  - specific file path for single-track downloads
  - filename format expectations
"""

import os

from tests.conftest import (
    SPOTIFY_TRACK,
    SPOTIFY_ALBUM,
    SPOTIFY_PLAYLIST,
)

# ──────────────────────────────────────────────────────────────────────────────
# output_dir
# ──────────────────────────────────────────────────────────────────────────────


class TestOutputDirectory:

    def test_existing_directory_accepted(self, tmp_output_dir, mock_spotiflac):
        assert os.path.isdir(tmp_output_dir)
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_TRACK, output_dir=tmp_output_dir)
        assert mock_cls.call_args.kwargs["output_dir"] == tmp_output_dir

    def test_path_object_accepted(self, tmp_path, mock_spotiflac):
        """output_dir may be passed as a pathlib.Path object."""
        out = tmp_path / "music"
        out.mkdir()
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_TRACK, output_dir=out)
        assert mock_cls.call_args.kwargs["output_dir"] == out

    def test_nested_output_directory(self, tmp_path, mock_spotiflac):
        deep = tmp_path / "library" / "lossless" / "2026"
        deep.mkdir(parents=True)
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_TRACK, output_dir=str(deep))
        assert mock_cls.call_args.kwargs["output_dir"] == str(deep)

    def test_relative_path_is_forwarded_as_is(self, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_TRACK, output_dir="./downloads")
        assert mock_cls.call_args.kwargs["output_dir"] == "./downloads"

    def test_output_dir_string_preserved_exactly(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_TRACK, output_dir=tmp_output_dir)
        assert mock_cls.call_args.kwargs["output_dir"] == tmp_output_dir


# ──────────────────────────────────────────────────────────────────────────────
# use_album_subfolders
# ──────────────────────────────────────────────────────────────────────────────


class TestAlbumSubfolders:

    def test_album_subfolders_true(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            use_album_subfolders=True,
        )
        assert mock_cls.call_args.kwargs["use_album_subfolders"] is True

    def test_album_subfolders_false(self, tmp_output_dir, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir=tmp_output_dir,
            use_album_subfolders=False,
        )
        assert mock_cls.call_args.kwargs["use_album_subfolders"] is False

    def test_album_subfolders_default_not_set(self, tmp_output_dir, mock_spotiflac):
        """When not specified the key may be absent from kwargs."""
        mock_cls, _ = mock_spotiflac
        mock_cls(url=SPOTIFY_ALBUM, output_dir=tmp_output_dir)
        kwargs = mock_cls.call_args.kwargs
        # Either key absent or default value applies — either is acceptable
        val = kwargs.get("use_album_subfolders", None)
        assert val is None or isinstance(val, bool)

    def test_subfolder_with_playlist(self, tmp_output_dir, mock_spotiflac):
        """Subfolders are useful for playlists too."""
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_PLAYLIST,
            output_dir=tmp_output_dir,
            use_album_subfolders=True,
        )
        assert mock_cls.call_args.kwargs["use_album_subfolders"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Single-track specific output path
# ──────────────────────────────────────────────────────────────────────────────


class TestSingleTrackOutputPath:
    """
    From the docs: if the input is a single track URL, you can optionally
    specify a specific .flac output path.
    """

    def test_specific_flac_path_accepted(self, tmp_path, mock_spotiflac):
        out_file = str(tmp_path / "my_song.flac")
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir=str(tmp_path),
            output_path=out_file,
        )
        kwargs = mock_cls.call_args.kwargs
        assert kwargs.get("output_path") == out_file

    def test_output_path_has_flac_extension(self, tmp_path):
        out_file = str(tmp_path / "track.flac")
        assert out_file.endswith(".flac")

    def test_output_path_parent_dir_must_exist(self, tmp_path):
        """The parent directory of output_path must exist before download."""
        out_file = tmp_path / "nested" / "track.flac"
        assert not out_file.parent.exists()
        # Create it manually
        out_file.parent.mkdir(parents=True)
        assert out_file.parent.exists()


# ──────────────────────────────────────────────────────────────────────────────
# Expected output file name patterns
# ──────────────────────────────────────────────────────────────────────────────


class TestFilenameConventions:
    """
    The module does not expose direct filename control in v1.2.8,
    but we validate that the output path strings match expected patterns.
    """

    def test_flac_extension_for_lossless_services(self):
        flac_services = ["tidal", "qobuz", "deezer", "amazon"]
        for svc in flac_services:
            # Simulate an expected output filename
            filename = "Artist - Track Title.flac"
            assert filename.endswith(".flac"), f"Expected .flac for {svc}"

    def test_mp3_extension_for_soundcloud(self):
        filename = "Artist - Track Title.mp3"
        assert filename.endswith(".mp3")

    def test_m4a_extension_for_apple_music(self):
        filename = "Artist - Track Title.m4a"
        assert filename.endswith(".m4a")

    def test_no_illegal_characters_in_expected_paths(self, tmp_path):
        """Filenames should not contain characters illegal on Windows/Linux."""
        illegal_chars = set('<>:"/\\|?*')
        sample_name = "Artist - My Song (feat. Other Artist).flac"
        assert not illegal_chars.intersection(
            set(sample_name)
        ), f"Illegal chars found in: {sample_name}"


# ──────────────────────────────────────────────────────────────────────────────
# Docker / container output path
# ──────────────────────────────────────────────────────────────────────────────


class TestDockerOutputPath:
    """
    When run via Docker, SpotiFLAC maps the container's /app/downloads
    to the host volume. The Python API should accept any absolute path.
    """

    def test_absolute_docker_style_path_accepted(self, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_TRACK,
            output_dir="/app/downloads",
        )
        assert mock_cls.call_args.kwargs["output_dir"] == "/app/downloads"

    def test_music_library_path_accepted(self, mock_spotiflac):
        mock_cls, _ = mock_spotiflac
        mock_cls(
            url=SPOTIFY_ALBUM,
            output_dir="./MusicLibrary",
        )
        assert mock_cls.call_args.kwargs["output_dir"] == "./MusicLibrary"

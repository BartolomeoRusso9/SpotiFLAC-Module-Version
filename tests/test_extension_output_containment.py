"""Tests for JSExtensionProvider._sanctioned_output — the check on the file
path an extension reports back.

Everything downstream of `_finalize_segments_async` treats that path as the
downloaded track: it is tagged, and a track-identity mismatch deletes it. The
path itself is just a string in the extension's result, so a wrong one (a bug
building it from unsanitised metadata, or worse) had the host write to and
delete a file the user never asked about.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from SpotiFLAC.extensions.provider import JSExtensionProvider

_AUDIO = b"ID3" + b"\x00" * 4096


@pytest.fixture
def provider():
    instance = JSExtensionProvider.__new__(JSExtensionProvider)
    instance.name = "ext:test-provider"
    return instance


@pytest.fixture
def output_path(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    return downloads / "song.flac"


def test_a_file_in_the_output_folder_is_accepted(provider, output_path):
    produced = output_path.parent / "song.flac"
    produced.write_bytes(_AUDIO)

    assert provider._sanctioned_output(str(produced), output_path) == produced.resolve()


def test_a_file_outside_the_output_folder_is_refused(provider, output_path, tmp_path):
    outsider = tmp_path / "elsewhere.flac"
    outsider.write_bytes(_AUDIO)

    assert provider._sanctioned_output(str(outsider), output_path) is None


def test_a_traversing_path_is_refused(provider, output_path, tmp_path):
    (tmp_path / "secrets.flac").write_bytes(_AUDIO)
    traversal = str(output_path.parent / ".." / "secrets.flac")

    assert provider._sanctioned_output(traversal, output_path) is None


def test_a_symlink_out_of_the_folder_is_refused(provider, output_path, tmp_path):
    outsider = tmp_path / "elsewhere.flac"
    outsider.write_bytes(_AUDIO)
    link = output_path.parent / "song.flac"
    link.symlink_to(outsider)

    # Resolved before the check: a link planted inside the output folder
    # must not stand in for a file outside it.
    assert provider._sanctioned_output(str(link), output_path) is None


def test_no_path_at_all_is_simply_nothing(provider, output_path):
    assert provider._sanctioned_output(None, output_path) is None
    assert provider._sanctioned_output("", output_path) is None


def test_an_outside_file_is_left_untouched_by_the_download(
    provider, output_path, tmp_path
):
    """The case the check exists for: an extension reports a whole, valid
    audio file that happens to live somewhere else. It must not become the
    download's result — otherwise the duration check passes, and a track
    identity mismatch then unlinks a file outside the output folder.
    """
    outsider = tmp_path / "not-ours.flac"
    outsider.write_bytes(_AUDIO)

    result = asyncio.run(
        provider._finalize_segments_async(
            {"success": True, "file_path": str(outsider)},
            output_path,
        )
    )

    # No usable audio: the download fails loudly instead of adopting a file
    # it was never given.
    assert result is None
    assert outsider.exists()
    assert outsider.read_bytes() == _AUDIO


def test_a_file_in_the_output_folder_still_finishes_the_download(provider, output_path):
    produced = output_path.parent / "song.flac"
    produced.write_bytes(_AUDIO)

    result = asyncio.run(
        provider._finalize_segments_async(
            {"success": True, "file_path": str(produced)},
            output_path,
        )
    )

    assert result is not None
    assert Path(result) == produced.resolve()


def test_the_output_folder_itself_is_not_a_downloaded_file(provider, output_path):
    result = asyncio.run(
        provider._finalize_segments_async(
            {"success": True, "file_path": str(output_path.parent)},
            output_path,
        )
    )
    assert result is None

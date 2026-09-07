"""Not converting an M4A to FLAC just to convert it back to M4A.

Some providers serve a FLAC stream inside an MP4 container. The extension
provider extracts it to a real `.flac` before handing the download on, which
is right when FLAC is what the run wants — and pure waste when it is not:
asking for `--transcode alac` produced m4a → flac → m4a, two full encodes to
land in the container the file already had.

Skipping the extraction is only safe because the pieces below agree on what
the file actually is. `.m4a` is a container, not a codec, and both the
orchestrator's "already in the target format" test and the bit-depth probe
used to answer for the container: the first read FLAC-in-MP4 as "already
ALAC" and returned it unconverted, the second read it as lossy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from SpotiFLAC.core.transcode import (
    _probe_source,
    already_in_target_format,
)
from SpotiFLAC.extensions.provider import _m4a_is_the_final_container


def _encode(tmp_path: Path, name: str, *args: str) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "1",
            *args,
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def flac_in_mp4(tmp_path: Path) -> Path:
    """What the provider actually downloads: FLAC audio, MP4 container."""
    return _encode(tmp_path, "track.m4a", "-c:a", "flac", "-f", "mp4")


@pytest.fixture
def real_alac(tmp_path: Path) -> Path:
    return _encode(tmp_path, "alac.m4a", "-c:a", "alac")


@pytest.fixture
def lossy_aac(tmp_path: Path) -> Path:
    return _encode(tmp_path, "aac.m4a", "-c:a", "aac", "-b:a", "128k")


# --- when the extraction is worth skipping --------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("alac", True),  # the reported case: .m4a in, .m4a out
        ("m4a", True),  # the same target, spelled the other way
        ("flac", False),  # extracting it is the whole point
        ("mp3", False),
        ("wav", False),
        (None, False),  # no transcode at all: the .flac is the deliverable
    ],
)
def test_only_an_m4a_target_makes_the_extraction_pointless(target, expected):
    assert _m4a_is_the_final_container(target, None) is expected


def test_a_decryption_key_always_keeps_the_extraction():
    """The remux is the only step that passes -decryption_key to ffmpeg.

    Skip it and the transcode is handed a file it cannot read.
    """
    assert _m4a_is_the_final_container("alac", "a-key") is False


def test_an_unknown_target_changes_nothing():
    assert _m4a_is_the_final_container("not-a-format", None) is False


# --- the checks that make skipping it safe --------------------------------


def test_flac_in_mp4_is_not_mistaken_for_alac(flac_in_mp4: Path):
    """Otherwise the transcode returns it untouched and it is never ALAC."""
    assert already_in_target_format(flac_in_mp4, "alac") is False


def test_real_alac_is_left_alone(real_alac: Path):
    assert already_in_target_format(real_alac, "alac") is True


def test_lossy_aac_in_an_m4a_is_not_alac_either(lossy_aac: Path):
    assert already_in_target_format(lossy_aac, "alac") is False


def test_flac_in_mp4_is_read_as_lossless(flac_in_mp4: Path):
    """It had lost nothing; warning that quality cannot be restored is wrong."""
    _, lossless = _probe_source(flac_in_mp4)
    assert lossless is True


def test_lossy_aac_is_still_read_as_lossy(lossy_aac: Path):
    _, lossless = _probe_source(lossy_aac)
    assert lossless is False


def test_the_extension_alone_never_decides(flac_in_mp4: Path, real_alac: Path):
    """Two files, one extension, opposite answers."""
    assert flac_in_mp4.suffix == real_alac.suffix == ".m4a"
    assert already_in_target_format(flac_in_mp4, "alac") is False
    assert already_in_target_format(real_alac, "alac") is True

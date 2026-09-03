"""A track number that is not a number must not cost a file its tags.

MusicBrainz answers with the number *printed on the release*, so the vinyl
pressing of "C,XOXO" reports Drake's "Uuugly" as "B2". That string reached
mutagen's MP4 writer, where int() raised — after audio.delete() had already
run. core/transcode.py caught the exception, kept the conversion and logged
a warning, and the finished ALAC file came out with no tags at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from SpotiFLAC.core.musicbrainz import _track_number
from SpotiFLAC.core.tagger import _embed_m4a, _tag_int

# --- parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7),
        (" 9 ", 9),
        ("", 0),
        (None, 0),
        # The one that broke: a vinyl designation. Its digits are still the
        # track's place on its side, so they beat throwing the field away.
        ("B2", 2),
        ("A1", 1),
        # "number/total", as written by half the taggers in existence.
        ("3/12", 3),
        # Nothing numeric anywhere.
        ("side one", 0),
    ],
)
def test_tag_int(value, expected) -> None:
    assert _tag_int(value) == expected


def test_tag_int_honours_its_default() -> None:
    """Disc numbers default to 1, not 0 — a file with no DISCNUMBER is on
    disc one, and writing 0 would say something false.
    """
    assert _tag_int("", 1) == 1
    assert _tag_int("side one", 1) == 1


# --- the MusicBrainz side ---------------------------------------------------


def test_a_vinyl_designation_falls_back_to_the_medium_position() -> None:
    assert _track_number({"number": "B2", "position": 9}) == "9"


def test_a_plain_number_is_kept_as_it_is() -> None:
    assert _track_number({"number": "20", "position": 20}) == "20"


def test_nothing_usable_yields_nothing() -> None:
    """Returning "" drops the key, so the provider's own track number
    survives instead of being overwritten with a designation.
    """
    assert _track_number({"number": "B2"}) == ""
    assert _track_number({}) == ""


# --- the writer that used to lose everything --------------------------------


@pytest.fixture
def m4a(tmp_path: Path) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "track.m4a"
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
            "-c:a",
            "alac",
            str(path),
        ],
        check=True,
    )
    return path


def test_a_vinyl_track_number_still_leaves_the_file_tagged(m4a: Path) -> None:
    from mutagen.mp4 import MP4

    _embed_m4a(
        m4a,
        {
            "TITLE": "Uuugly",
            "ARTIST": "Drake",
            "ALBUM": "C,XOXO",
            "TRACKNUMBER": "B2",
            "TRACKTOTAL": "17",
            "DISCNUMBER": "1",
            "DISCTOTAL": "1",
        },
        None,
        None,
        "",
    )

    tags = MP4(str(m4a)).tags
    assert tags["\xa9nam"] == ["Uuugly"]
    assert tags["\xa9ART"] == ["Drake"]
    assert tags["\xa9alb"] == ["C,XOXO"]
    assert tags["trkn"] == [(2, 17)]


def test_an_unparseable_bpm_does_not_take_the_rest_with_it(m4a: Path) -> None:
    """BPM stays strict where the track number is lenient: a tempo read out
    of surrounding prose would be a number nobody measured. It is dropped,
    and dropping it costs the other tags nothing.
    """
    from mutagen.mp4 import MP4

    _embed_m4a(m4a, {"TITLE": "Uuugly", "BPM": "unknown"}, None, None, "")

    tags = MP4(str(m4a)).tags
    assert tags["\xa9nam"] == ["Uuugly"]
    assert "tmpo" not in tags

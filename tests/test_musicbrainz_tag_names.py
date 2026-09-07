"""MusicBrainz IDs have to be spelled the way each container spells them.

"MUSICBRAINZ_ALBUMID" is the *Vorbis* name — correct for FLAC and OGG, and
wrong everywhere else. Handed to the M4A writer's freeform fallback it came
out as "----:com.apple.iTunes:MUSICBRAINZ_ALBUMID", and to the ID3 writer as
"TXXX:MUSICBRAINZ_ALBUMID": valid frames that no reader looks for, so a
downloaded file carried every MusicBrainz ID and appeared to Picard,
foobar2000, beets and MusicBee to carry none.

The expected names below were taken from what mutagen's own EasyMP4 and
EasyID3 write, not from memory — and they are not derivable from each
other: the country field is "MusicBrainz Release Country" in MP4 and
"MusicBrainz Album Release Country" in ID3, and ID3 puts the recording ID
in a UFID frame rather than a TXXX of its own.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from SpotiFLAC.core.tagger import _embed_id3, _embed_m4a, read_embedded_tags

_MB_TAGS = {
    "TITLE": "Bottiglie Privè",
    "ARTIST": "Sfera Ebbasta",
    "ALBUM": "Famoso",
    "MUSICBRAINZ_TRACKID": "3de28228-278d-463e-920f-50c655f983aa",
    "MUSICBRAINZ_ALBUMID": "61cc1e27-e02f-402f-8f70-ed39d8ef29d0",
    "MUSICBRAINZ_ARTISTID": "2380d705-1af6-4fab-b408-d9c095806a46",
    "MUSICBRAINZ_ALBUMARTISTID": "2380d705-1af6-4fab-b408-d9c095806a46",
    "MUSICBRAINZ_RELEASEGROUPID": "7916b0f6-9f85-4827-851b-c83f608c50fe",
    "RELEASESTATUS": "Official",
    "RELEASETYPE": "Album",
    "RELEASECOUNTRY": "IT",
}

_MB_KEYS = [k for k in _MB_TAGS if k.startswith(("MUSICBRAINZ_", "RELEASE"))]


def _silent(tmp_path: Path, name: str, codec: str) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / name
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "1", "-c:a", codec, str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def m4a(tmp_path: Path) -> Path:
    return _silent(tmp_path, "track.m4a", "alac")


@pytest.fixture
def mp3(tmp_path: Path) -> Path:
    return _silent(tmp_path, "track.mp3", "libmp3lame")


# --- the names on disk ----------------------------------------------------


def test_m4a_uses_the_atom_names_readers_look_for(m4a: Path) -> None:
    from mutagen.mp4 import MP4

    _embed_m4a(m4a, dict(_MB_TAGS), None, None, "")
    written = set(MP4(str(m4a)).tags)

    assert {
        "----:com.apple.iTunes:MusicBrainz Track Id",
        "----:com.apple.iTunes:MusicBrainz Album Id",
        "----:com.apple.iTunes:MusicBrainz Artist Id",
        "----:com.apple.iTunes:MusicBrainz Album Artist Id",
        "----:com.apple.iTunes:MusicBrainz Release Group Id",
        "----:com.apple.iTunes:MusicBrainz Album Status",
        "----:com.apple.iTunes:MusicBrainz Album Type",
        "----:com.apple.iTunes:MusicBrainz Release Country",
    } <= written

    # And none of the underscored spellings are left behind.
    assert not [k for k in written if "MUSICBRAINZ_" in k]


def test_mp3_uses_the_frames_readers_look_for(mp3: Path) -> None:
    from mutagen.id3 import ID3

    _embed_id3(mp3, dict(_MB_TAGS), None, None, "")
    written = set(ID3(str(mp3)).keys())

    assert {
        "TXXX:MusicBrainz Album Id",
        "TXXX:MusicBrainz Artist Id",
        "TXXX:MusicBrainz Album Artist Id",
        "TXXX:MusicBrainz Release Group Id",
        "TXXX:MusicBrainz Album Status",
        "TXXX:MusicBrainz Album Type",
        "TXXX:MusicBrainz Album Release Country",
    } <= written
    assert not [k for k in written if "MUSICBRAINZ_" in k]


def test_the_mp3_recording_id_goes_in_a_ufid_frame(mp3: Path) -> None:
    """ID3's own field for it. A TXXX of that name is not read as one."""
    from mutagen.id3 import ID3

    _embed_id3(mp3, dict(_MB_TAGS), None, None, "")
    tags = ID3(str(mp3))

    assert "UFID:http://musicbrainz.org" in tags
    frame = tags["UFID:http://musicbrainz.org"]
    assert frame.data.decode() == _MB_TAGS["MUSICBRAINZ_TRACKID"]
    assert "TXXX:MusicBrainz Track Id" not in tags


# --- round trip -----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["m4a", "mp3"])
def test_every_id_survives_a_round_trip(fmt, request) -> None:
    path = request.getfixturevalue(fmt)
    writer = _embed_m4a if fmt == "m4a" else _embed_id3
    writer(path, dict(_MB_TAGS), None, None, "")

    read_back = read_embedded_tags(path).tags
    assert {k: read_back.get(k) for k in _MB_KEYS} == {
        k: _MB_TAGS[k] for k in _MB_KEYS
    }


# --- files written before the mapping existed -----------------------------


def test_an_m4a_written_the_old_way_is_still_read(m4a: Path) -> None:
    """Every file already in the user's library carries the old spelling."""
    from mutagen.mp4 import MP4

    audio = MP4(str(m4a))
    audio["----:com.apple.iTunes:MUSICBRAINZ_ALBUMID"] = [b"old-album"]
    audio["----:com.apple.iTunes:RELEASETYPE"] = [b"Album"]
    audio.save()

    read_back = read_embedded_tags(m4a).tags
    assert read_back["MUSICBRAINZ_ALBUMID"] == "old-album"
    assert read_back["RELEASETYPE"] == "Album"


def test_an_mp3_written_the_old_way_is_still_read(mp3: Path) -> None:
    from mutagen.id3 import ID3, TXXX

    tags = ID3(str(mp3))
    tags.add(TXXX(encoding=3, desc="MUSICBRAINZ_ALBUMID", text="old-album"))
    tags.add(TXXX(encoding=3, desc="MUSICBRAINZ_TRACKID", text="old-track"))
    tags.save()

    read_back = read_embedded_tags(mp3).tags
    assert read_back["MUSICBRAINZ_ALBUMID"] == "old-album"
    assert read_back["MUSICBRAINZ_TRACKID"] == "old-track"


def test_another_owners_ufid_is_not_read_as_a_musicbrainz_id(mp3: Path) -> None:
    from mutagen.id3 import ID3, UFID

    tags = ID3(str(mp3))
    tags.add(UFID(owner="http://example.org", data=b"not-musicbrainz"))
    tags.save()

    assert "MUSICBRAINZ_TRACKID" not in read_embedded_tags(mp3).tags

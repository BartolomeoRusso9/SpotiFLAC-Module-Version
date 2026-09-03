"""Writing the lyrics out as .lrc files beside the download.

No player on macOS renders a word-by-word lyric out of an embedded tag:
Apple Music strips the inline timing and shows flat text — which is what
"[00:08.75]Sento unra-ta-ta- ta" looks like on screen when the tag actually
reads "[00:08.75]<00:08.75>Sento <00:09.05>un<00:09.22>ra-...". The synced
display comes from an overlay app reading an .lrc from disk, so the file has
to exist.

Two layouts, because the two kinds of player disagree: one pairs a sidecar
with the audio by filename, the other looks lyrics up as "Artist - Title".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from SpotiFLAC import downloader as dl
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

WORD_BY_WORD = (
    "[ti:RATATA]\n"
    "[ar:Capo Plaza]\n"
    "\n"
    "[00:08.75]<00:08.75>Sento <00:09.05>un<00:09.22>ra-<00:09.41>ta- <00:09.90>ta"
)


def _track() -> TrackMetadata:
    return TrackMetadata(
        id="x",
        title="RATATA",
        artists="Capo Plaza",
        artist_names=["Capo Plaza"],
        album="20 (Deluxe Edition)",
        album_artist="Capo Plaza",
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "RATATA - Capo Plaza.m4a"
    path.write_bytes(b"not really audio")
    return path


@pytest.fixture
def lyrics(monkeypatch):
    """Stand in for reading the tag back off the finished file."""
    holder = {"value": WORD_BY_WORD}

    class _Tags:
        def __init__(self, text):
            self.lyrics = text

    monkeypatch.setattr(
        "SpotiFLAC.core.tagger.read_embedded_tags",
        lambda path, include_cover=True: _Tags(holder["value"]),
    )
    return holder


def _run(audio: Path, **opts_kwargs):
    opts = dl.DownloadOptions(output_dir=str(audio.parent), **opts_kwargs)
    asyncio.run(
        dl._write_lrc_sidecars_async(
            DownloadResult.ok("tidal", str(audio)),
            _track(),
            opts,
        ),
    )


def test_the_sidecar_takes_the_audio_files_name(audio, lyrics) -> None:
    _run(audio, save_lrc=True)

    sidecar = audio.with_suffix(".lrc")
    assert sidecar.exists()
    assert "<00:09.05>" in sidecar.read_text(encoding="utf-8")


def test_the_library_copy_is_artist_first(audio, lyrics, tmp_path) -> None:
    """ "Artist - Title", the reverse of this project's default filename
    format — which is exactly why it cannot reuse the audio file's stem.
    """
    library = tmp_path / "Lyrics"
    _run(audio, lrc_library_dir=str(library))

    assert (library / "Capo Plaza - RATATA.lrc").exists()
    assert not audio.with_suffix(".lrc").exists()


def test_both_layouts_at_once(audio, lyrics, tmp_path) -> None:
    library = tmp_path / "Lyrics"
    _run(audio, save_lrc=True, lrc_library_dir=str(library))

    assert audio.with_suffix(".lrc").exists()
    assert (library / "Capo Plaza - RATATA.lrc").exists()


def test_the_library_folder_is_created(audio, lyrics, tmp_path) -> None:
    library = tmp_path / "nested" / "Lyrics"
    _run(audio, lrc_library_dir=str(library))

    assert (library / "Capo Plaza - RATATA.lrc").exists()


def test_nothing_is_written_when_neither_is_asked_for(audio, lyrics) -> None:
    _run(audio)
    assert not audio.with_suffix(".lrc").exists()


def test_a_track_without_lyrics_leaves_no_empty_file(audio, lyrics) -> None:
    lyrics["value"] = "   "
    _run(audio, save_lrc=True)
    assert not audio.with_suffix(".lrc").exists()


def test_an_unwritable_destination_does_not_fail_the_download(
    audio, lyrics, tmp_path
) -> None:
    """The audio is downloaded and tagged by this point. A lyrics file that
    cannot be written is worth a log line, not a failed track.
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory")

    _run(audio, lrc_library_dir=str(blocked))  # must not raise

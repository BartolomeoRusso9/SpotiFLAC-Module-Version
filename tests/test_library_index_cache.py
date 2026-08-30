"""Reusing a file's parsed tags while the file has not changed.

index_audio_files() runs before every download to answer "do I already have
this?", and it read the tags of every file in the library each time. The
saving is real but modest — measured at 0.11 ms per file against 0.0013 ms
for a stat, so roughly two seconds on a twenty-thousand-track library, not
the minutes a full decode would cost. mutagen reads the header, not the
audio.

What matters more than the speed is that the cache must never answer
*wrongly*: a stale hit means concluding a track is already downloaded when
it is not, or matching the wrong file. Every test below is really about
that.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from SpotiFLAC.core.library_index_cache import (
    CachedTags,
    LibraryIndexCache,
    cache_path_for,
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A real FLAC with real tags, in a directory with its own cache."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg to make a tagged FLAC")

    monkeypatch.setattr(
        "SpotiFLAC.core.library_index_cache._CACHE_DIR", tmp_path / "cache"
    )
    music = tmp_path / "music"
    music.mkdir()
    track = music / "song.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=1",
            "-metadata",
            "TITLE=Song",
            "-metadata",
            "ARTIST=Artist",
            "-metadata",
            "ALBUM=Album",
            "-metadata",
            "ISRC=GBAYE0601498",
            "-y",
            str(track),
            "-loglevel",
            "error",
        ],
        check=True,
    )
    return music, track


# --- the cache must not answer wrongly -------------------------------------


def test_a_touched_file_is_re_read(library) -> None:
    """The whole risk: a stale hit would report the tags of a file that has
    since been retagged.
    """
    music, track = library
    cache = LibraryIndexCache(music)
    cache.put(track, CachedTags(title="Old"))
    cache.save()

    os.utime(track, (0, 0))

    assert LibraryIndexCache(music).get(track) is None


def test_a_file_whose_size_changed_is_re_read(library) -> None:
    """mtime alone can be preserved across a rewrite; the size catches it."""
    music, track = library
    cache = LibraryIndexCache(music)
    cache.put(track, CachedTags(title="Old"))
    cache.save()
    stat = track.stat()

    track.write_bytes(track.read_bytes() + b"padding")
    os.utime(track, (stat.st_atime, stat.st_mtime))

    assert LibraryIndexCache(music).get(track) is None


def test_a_missing_file_is_never_a_hit(library) -> None:
    music, track = library
    cache = LibraryIndexCache(music)
    cache.put(track, CachedTags(title="Song"))
    cache.save()
    track.unlink()

    assert LibraryIndexCache(music).get(track) is None


def test_an_unchanged_file_is_a_hit(library) -> None:
    music, track = library
    cache = LibraryIndexCache(music)
    cache.put(track, CachedTags(title="Song", isrc="GBAYE0601498"))
    cache.save()

    reopened = LibraryIndexCache(music)
    cached = reopened.get(track)
    assert cached is not None
    assert cached.title == "Song"
    assert cached.isrc == "GBAYE0601498"


# --- a broken cache costs a re-read and nothing else -----------------------


@pytest.mark.parametrize(
    "contents", ["", "not json", "[]", '{"version": 999, "entries": {}}']
)
def test_an_unusable_cache_file_is_ignored(library, contents) -> None:
    music, track = library
    path = cache_path_for(music)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)

    assert LibraryIndexCache(music).get(track) is None  # no exception


def test_an_unwritable_cache_does_not_raise(library, monkeypatch) -> None:
    music, track = library
    cache = LibraryIndexCache(music)
    cache.put(track, CachedTags(title="Song"))
    monkeypatch.setattr(
        "SpotiFLAC.core.atomic_io.write_json_atomic",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    cache.save()  # must not raise


def test_entries_for_files_that_are_gone_are_dropped(library) -> None:
    """Otherwise the file grows forever as tracks are renamed or deleted."""
    music, track = library
    cache = LibraryIndexCache(music)
    cache.put(track, CachedTags(title="Song"))
    cache.put(music / "deleted.flac", CachedTags(title="Ghost"))
    cache.save()

    # A second walk that only sees the surviving file.
    second = LibraryIndexCache(music)
    second.get(track)
    second.save()

    import json

    entries = json.loads(cache_path_for(music).read_text())["entries"]
    assert str(track) in entries
    assert str(music / "deleted.flac") not in entries


# --- the index itself ------------------------------------------------------


def test_the_index_gives_the_same_answer_with_and_without_the_cache(library) -> None:
    """The cache is an optimisation. If it changes the answer it is a bug,
    not a speed-up.
    """
    from SpotiFLAC.core.playlist_sync import index_audio_files

    music, _ = library
    uncached = index_audio_files(music, use_cache=False)
    index_audio_files(music)  # populate
    cached = index_audio_files(music)

    assert cached["__isrc__"].keys() == uncached["__isrc__"].keys()
    assert cached["__identity__"].keys() == uncached["__identity__"].keys()


def test_two_directories_do_not_share_a_cache(tmp_path) -> None:
    assert cache_path_for(tmp_path / "a") != cache_path_for(tmp_path / "b")

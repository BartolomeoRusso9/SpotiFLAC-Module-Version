from __future__ import annotations

from types import SimpleNamespace

from mornye.core import smb_library_scan as scan
from mornye.core.network_destinations import validate_profile

PROFILE = validate_profile(
    {
        "id": "destination1234",
        "name": "Home NAS",
        "host": "nas.example.net",
        "share": "Music",
        "subdirectory": "Albums",
        "username": "music-user",
    }
)


def test_remote_stream_tags_are_parsed_with_the_remote_filename(monkeypatch) -> None:
    captured = {}

    class Audio:
        tags = {
            "title": ["Song"],
            "artist": ["Artist One", "Artist Two"],
            "album": ["Album"],
            "genre": ["Rock", "Pop"],
            "date": ["1997-06-01"],
        }
        info = SimpleNamespace(length=201.25)

    def fake_mutagen_file(filething, easy):
        captured["name"] = filething.name
        captured["easy"] = easy
        return Audio()

    class Backend:
        def __init__(self, *_args):
            pass

        def open_audio_file(self, path):
            captured["path"] = path
            return _Stream()

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, *_args):
            return b""

        def seek(self, *_args):
            return 0

    monkeypatch.setattr(scan, "SMBBackend", Backend)
    monkeypatch.setattr(scan.mutagen, "File", fake_mutagen_file)

    record = scan._read_remote_file(
        Backend(),
        {"path": r"\\nas.example.net\Music\Albums\song.flac", "size": 123},
    )

    assert record == {
        "title": "Song",
        "artist": "Artist One; Artist Two",
        "album": "Album",
        "genre": "Rock; Pop",
        "release_year": "1997",
        "duration_ms": 201250,
        "format": "flac",
        "bytes": 123,
    }
    assert captured["easy"] is True
    assert captured["name"].endswith("song.flac")


def test_scan_builds_stats_report_without_touching_download_log(monkeypatch) -> None:
    files = [
        {"path": r"\\nas.example.net\Music\a.flac", "size": 100},
        {"path": r"\\nas.example.net\Music\b.m4a", "size": 200},
        {"path": r"\\nas.example.net\Music\broken.flac", "size": 50},
    ]
    records = {
        files[0]["path"]: {
            "title": "First",
            "artist": "Queen",
            "album": "Opera",
            "genre": "Rock",
            "release_year": "1975",
            "duration_ms": 200000,
            "format": "flac",
            "bytes": 100,
        },
        files[1]["path"]: {
            "title": "Second",
            "artist": "Queen",
            "album": "Opera",
            "genre": "",
            "release_year": "",
            "duration_ms": 180000,
            "format": "m4a",
            "bytes": 200,
        },
        files[2]["path"]: None,
    }

    class Backend:
        def __init__(self, profile, username, password):
            assert profile is PROFILE
            assert (username, password) == ("music-user", "secret")

        def list_audio_files(self):
            return files

    monkeypatch.setattr(scan, "SMBBackend", Backend)
    monkeypatch.setattr(
        scan, "_read_remote_file", lambda _backend, item: records[item["path"]]
    )
    progress = []

    report = scan.scan_smb_library(
        PROFILE,
        "music-user",
        "secret",
        progress=lambda done, total: progress.append((done, total)),
    )

    assert report["source"] == "smb"
    assert report["source_name"] == "Home NAS"
    assert report["scan"] == {"files": 3, "readable": 2, "unreadable": 1}
    assert report["totals"]["tracks"] == 2
    assert report["totals"]["bytes"] == 350
    assert report["totals"]["listening_ms"] == 380000
    assert report["top_artists"] == [
        {"name": "Queen", "tracks": 2, "share": 1.0},
    ]
    assert report["top_tracks"] == [
        {"name": "First", "artist": "Queen", "tracks": 1},
        {"name": "Second", "artist": "Queen", "tracks": 1},
    ]
    assert report["top_genres"]["entries"][0]["name"] == "Rock"
    assert report["decades"]["entries"][0]["name"] == "1970s"
    assert {entry["name"] for entry in report["formats"]} == {"flac", "m4a"}
    assert progress == [(3, 3)]


def test_empty_scan_reports_zero_files() -> None:
    report = scan._build_stats("Home NAS", [], [], 0)

    assert report["scan"] == {"files": 0, "readable": 0, "unreadable": 0}
    assert report["totals"]["tracks"] == 0
    assert report["top_artists"] == []

from mornye.tui.library_view import LibraryPanel


def test_smb_report_is_a_compact_library_summary() -> None:
    report = {
        "source": "smb",
        "source_name": "Home NAS",
        "scan": {"files": 3, "readable": 2, "unreadable": 1},
        "totals": {
            "tracks": 2,
            "artists": 1,
            "albums": 1,
            "bytes": 350,
            "listening_ms": 380000,
        },
        "top_artists": [{"name": "Queen", "artist": "", "tracks": 2}],
        "top_albums": [{"name": "Opera", "artist": "Queen", "tracks": 2}],
        "top_genres": {"entries": [{"name": "Rock", "tracks": 1}]},
        "top_tracks": [{"name": "Bohemian Rhapsody", "artist": "Queen", "tracks": 1}],
        "decades": {"entries": [{"name": "1970s", "tracks": 1}]},
        "formats": [{"name": "flac", "tracks": 1}],
    }

    rendered = LibraryPanel._format_report(report)

    assert "Home NAS · SMB library" in rendered
    assert "2 readable track(s) · 3 audio file(s) · 1 unreadable" in rendered
    assert "Queen (2)" in rendered
    assert "Opera — Queen (2)" in rendered
    assert "Bohemian Rhapsody — Queen (1)" in rendered
    assert "1970s (1)" in rendered
    assert "FLAC (1)" in rendered
    assert "Providers" not in rendered


def test_smb_report_does_not_show_empty_artist_suffix() -> None:
    report = {
        "source_name": "Home NAS",
        "source": "smb",
        "scan": {"files": 1, "readable": 1, "unreadable": 0},
        "totals": {
            "tracks": 1,
            "artists": 1,
            "albums": 1,
            "bytes": 120,
            "listening_ms": 210000,
        },
        "top_artists": [{"name": "Unknown artist", "tracks": 1}],
        "top_albums": [{"name": "Unknown album", "tracks": 1}],
        "top_tracks": [{"name": "Mysterious Song", "artist": "", "tracks": 1}],
        "top_genres": {"entries": [{"name": "Rock", "tracks": 1}]},
        "decades": {"entries": [{"name": "1990s", "tracks": 1}]},
        "formats": [{"name": "mp3", "tracks": 1}],
    }

    rendered = LibraryPanel._format_report(report)

    assert "Mysterious Song (1)" in rendered
    assert "—  (1)" not in rendered

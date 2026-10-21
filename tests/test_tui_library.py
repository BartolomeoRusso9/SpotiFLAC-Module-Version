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
        "decades": {"entries": [{"name": "1970s", "tracks": 1}]},
        "formats": [{"name": "flac", "tracks": 1}],
    }

    rendered = LibraryPanel._format_report(report)

    assert "Home NAS · SMB library" in rendered
    assert "2 readable track(s) · 3 audio file(s) · 1 unreadable" in rendered
    assert "Queen (2)" in rendered
    assert "Opera — Queen (2)" in rendered
    assert "1970s (1)" in rendered
    assert "FLAC (1)" in rendered
    assert "Providers" not in rendered

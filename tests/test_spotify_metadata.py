from SpotiFLAC.providers.spotify_metadata import parse_spotify_url


def test_parse_spotify_track_url():
    parsed = parse_spotify_url("https://open.spotify.com/track/12345ABCDE?si=abc")
    assert parsed == {"type": "track", "id": "12345ABCDE"}


def test_parse_spotify_playlist_url():
    parsed = parse_spotify_url("https://open.spotify.com/playlist/PL12345ABCDE")
    assert parsed == {"type": "playlist", "id": "PL12345ABCDE"}


def test_parse_spotify_artist_discography_url():
    parsed = parse_spotify_url("https://open.spotify.com/artist/1A2B3C4D5E6F7G8H9I0J/discography")
    assert parsed["type"] == "artist_discography"
    assert parsed["id"] == "1A2B3C4D5E6F7G8H9I0J"
    assert parsed.get("group") == "all"

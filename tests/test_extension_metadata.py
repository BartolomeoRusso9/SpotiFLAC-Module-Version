"""`core/extension_metadata.py` — Melon, Bugs and Genie links and searches.

These catalogues are only reachable through extensions written for the
mobile app. What is worth pinning is the routing around them: a link finds
the extension by the site its manifest may reach, the extension's answer
becomes TrackMetadata with links that resolve back to the same site, and the
ISRC is borrowed from Spotify without letting a script mismatch between
"아이유" and "IU" throw a correct match away — or a wrong one in.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from SpotiFLAC.core.errors import SpotiflacError
from SpotiFLAC.core.extension_metadata import (
    SITES,
    ExtensionMetadataClient,
    attach_isrcs,
    metadata_sources,
    parse_catalogue_url,
    site_for_extension,
    to_track,
)
from SpotiFLAC.core.models import TrackMetadata

MELON = next(s for s in SITES if s.key == "melon")


def _ext(name, hosts, types=("metadata_provider",), display="Melon Music"):
    return SimpleNamespace(
        name=name,
        display_name=display,
        types=list(types),
        manifest={"type": list(types), "permissions": {"network": list(hosts)}},
    )


class _Manager:
    def __init__(self, *exts):
        self._exts = list(exts)

    def list_installed(self):
        return self._exts


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, site, kind, item_id",
    [
        (
            "https://www.melon.com/song/detail.htm?songId=34431086",
            "melon",
            "track",
            "34431086",
        ),
        (
            "https://www.melon.com/album/detail.htm?albumId=10123637",
            "melon",
            "album",
            "10123637",
        ),
        ("https://m2.melon.com/album/detail.htm?albumId=11", "melon", "album", "11"),
        (
            "https://www.melon.com/mymusic/dj/mymusicdjplaylistview_inform.htm?plylstSeq=4455",
            "melon",
            "playlist",
            "4455",
        ),
        ("https://music.bugs.co.kr/track/6143370", "bugs", "track", "6143370"),
        (
            "https://music.bugs.co.kr/album/20398563?wl_ref=list",
            "bugs",
            "album",
            "20398563",
        ),
        (
            "https://www.genie.co.kr/detail/songInfo?xgnm=97498843",
            "genie",
            "track",
            "97498843",
        ),
        (
            "https://www.genie.co.kr/detail/albumInfo?axnm=81234567",
            "genie",
            "album",
            "81234567",
        ),
    ],
)
def test_links_are_read_per_site(url, site, kind, item_id) -> None:
    link = parse_catalogue_url(url)
    assert (link.site.key, link.kind, link.item_id) == (site, kind, item_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com/album/abc",
        "https://www.melon.com/chart/index.htm",  # a known site, but no item
        "https://example.com/?songId=123",  # the parameter, on another host
        "https://melon.com.evil.example/song/detail.htm?songId=1",
    ],
)
def test_anything_else_is_left_alone(url) -> None:
    assert parse_catalogue_url(url) is None


def test_the_extension_is_found_by_the_hosts_it_may_reach() -> None:
    """Not by id: a fork of the Melon extension under another name is still
    the Melon extension."""
    assert site_for_extension(_ext("my-melon-fork", ["www.melon.com"])).key == "melon"
    assert site_for_extension(_ext("x", ["api.song.link"])) is None
    # A download provider that happens to reach the same host is not asked.
    assert (
        site_for_extension(_ext("x", ["melon.com"], types=["download_provider"]))
        is None
    )


def test_a_link_without_its_extension_says_what_is_missing() -> None:
    with pytest.raises(SpotiflacError) as err:
        ExtensionMetadataClient.for_url(
            "https://music.bugs.co.kr/track/1",
            manager=_Manager(_ext("melon-music", ["melon.com"])),
        )
    assert "Bugs" in str(err.value)


def test_sources_list_spotify_first_then_each_catalogue() -> None:
    sources = metadata_sources(
        _Manager(
            _ext("melon-music", ["melon.com"]),
            _ext("deezer", ["deezer.com"], ["download_provider"]),
        )
    )
    assert [s["id"] for s in sources] == ["spotify", "melon-music"]


# ---------------------------------------------------------------------------
# The extension's answer
# ---------------------------------------------------------------------------

ALBUM = {
    "id": "10123637",
    "type": "album",
    "name": "Palette",
    "artists": "아이유",
    "cover_url": "https://cdn.melon.co.kr/album.jpg",
    "tracks": [
        {
            "id": "30512671",
            "name": "이 지금",
            "artists": "아이유",
            "album_name": "",
            "duration_ms": 0,
        },
        {
            "id": "30512672",
            "name": "Palette (Feat. G-DRAGON)",
            "artists": "아이유",
            "duration_ms": 217000,
        },
    ],
}


def test_album_tracks_carry_numbering_and_links_back_to_the_site() -> None:
    tracks = [
        to_track(
            item, MELON, position=i, total=2, collection={**ALBUM, "type": "album"}
        )
        for i, item in enumerate(ALBUM["tracks"], start=1)
    ]
    first = tracks[0]
    assert first.title == "이 지금"
    assert first.album == "Palette"  # filled from the album it came in
    assert first.album_artist == "아이유"
    assert (first.track_number, first.total_tracks) == (1, 2)
    assert first.external_url == "https://www.melon.com/song/detail.htm?songId=30512671"
    assert first.album_url == "https://www.melon.com/album/detail.htm?albumId=10123637"
    assert first.cover_url == "https://cdn.melon.co.kr/album.jpg"
    # Links it hands out resolve back to the same item.
    assert parse_catalogue_url(first.external_url).item_id == "30512671"


def test_a_playlist_position_is_not_a_track_number() -> None:
    track = to_track(
        {"id": "1", "name": "Song", "artists": "A"},
        MELON,
        position=7,
        total=20,
        collection={"type": "playlist", "name": "DJ mix"},
    )
    assert track.track_number == 0
    # Nor is the playlist's name an album name.
    assert track.album != "DJ mix"


def test_an_item_without_id_or_title_is_skipped() -> None:
    assert to_track({"name": "no id"}, MELON) is None
    assert to_track({"id": "1"}, MELON) is None


def _client(responses: dict) -> ExtensionMetadataClient:
    class _Provider:
        def _call(self, method, *args):
            return responses[method]

    return ExtensionMetadataClient(
        "melon-music",
        MELON,
        provider_factory=lambda name: _Provider(),
        match_isrcs=False,
    )


def test_an_album_link_resolves_to_its_tracks() -> None:
    name, tracks, cover = asyncio.run(
        _client({"getAlbum": ALBUM}).get_url_async(
            "https://www.melon.com/album/detail.htm?albumId=10123637"
        )
    )
    assert name == "Palette"
    assert [t.title for t in tracks] == ["이 지금", "Palette (Feat. G-DRAGON)"]
    assert cover == "https://cdn.melon.co.kr/album.jpg"


def test_a_track_link_unwraps_the_track() -> None:
    _name, tracks, _cover = asyncio.run(
        _client(
            {"getTrack": {"success": True, "track": ALBUM["tracks"][1]}}
        ).get_url_async("https://www.melon.com/song/detail.htm?songId=30512672")
    )
    assert tracks[0].duration_ms == 217000


def test_an_extension_failure_is_an_error_not_an_empty_album() -> None:
    with pytest.raises(SpotiflacError):
        asyncio.run(
            _client({"getAlbum": None}).get_url_async(
                "https://www.melon.com/album/detail.htm?albumId=1"
            )
        )


def test_search_returns_tracks_and_the_albums_they_come_from() -> None:
    found = [
        {
            "id": "1",
            "name": "밤편지",
            "artists": "아이유",
            "album_id": "A",
            "album_name": "Night",
        },
        {
            "id": "2",
            "name": "사랑이 잘",
            "artists": "아이유",
            "album_id": "A",
            "album_name": "Night",
        },
    ]
    results = asyncio.run(_client({"searchTracks": found}).search_async("아이유"))
    assert [t.title for t in results["tracks"]] == ["밤편지", "사랑이 잘"]
    assert results["albums"] == [
        {
            "id": "A",
            "name": "Night",
            "artists": "아이유",
            "cover_url": "",
            "external_url": "https://www.melon.com/album/detail.htm?albumId=A",
        }
    ]


# ---------------------------------------------------------------------------
# Borrowing the ISRC
# ---------------------------------------------------------------------------


def _track(title, artists="아이유", duration_ms=0) -> TrackMetadata:
    return TrackMetadata(
        id=f"melon:{title}",
        title=title,
        artists=artists,
        album="",
        album_artist=artists,
        duration_ms=duration_ms,
    )


def _candidate(title, artists="IU", duration_ms=0, spotify_id="sp1"):
    return SimpleNamespace(
        id=spotify_id,
        title=title,
        artists=artists,
        first_artist=artists,
        album="",
        duration_ms=duration_ms,
        isrc="",
        release_date="2017-05-15",
    )


class _Spotify:
    def __init__(self, candidates):
        self.candidates = candidates
        self.queries = []

    async def search_tracks_async(self, query, limit=10):
        self.queries.append(query)
        return self.candidates


class _Isrc:
    async def get_isrc_async(self, track_id):
        return {"sp1": "KRA381700512"}.get(track_id, "")


def test_the_same_song_under_a_romanised_artist_still_matches() -> None:
    spotify = _Spotify([_candidate("밤편지", duration_ms=253000)])
    [track] = asyncio.run(
        attach_isrcs(
            [_track("밤편지", duration_ms=253500)], spotify=spotify, isrc_helper=_Isrc()
        )
    )
    assert track.isrc == "KRA381700512"
    assert track.title == "밤편지"  # the catalogue's own names are kept
    assert spotify.queries == ["밤편지 아이유"]


def test_a_same_titled_song_of_another_length_is_not_taken() -> None:
    spotify = _Spotify([_candidate("밤편지", duration_ms=180000)])
    [track] = asyncio.run(
        attach_isrcs(
            [_track("밤편지", duration_ms=253000)], spotify=spotify, isrc_helper=_Isrc()
        )
    )
    assert track.isrc == ""


def test_a_different_title_is_not_taken() -> None:
    spotify = _Spotify([_candidate("Blueming")])
    [track] = asyncio.run(
        attach_isrcs([_track("밤편지")], spotify=spotify, isrc_helper=_Isrc())
    )
    assert track.isrc == ""


def test_tracks_that_already_have_an_isrc_cost_no_lookup() -> None:
    spotify = _Spotify([])
    known = _track("밤편지").model_copy(update={"isrc": "KRA000"})
    assert (
        asyncio.run(attach_isrcs([known], spotify=spotify, isrc_helper=_Isrc()))[0].isrc
        == "KRA000"
    )
    assert spotify.queries == []


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_the_tracklist_sends_a_catalogue_link_to_its_extension(monkeypatch) -> None:
    import SpotiFLAC.core.extension_metadata as module
    from SpotiFLAC.core.tracklist import metadata_client_for

    monkeypatch.setattr(
        module,
        "catalogue_extensions",
        lambda manager=None: [(_ext("melon-music", ["melon.com"]), MELON)],
    )
    client = metadata_client_for("https://www.melon.com/album/detail.htm?albumId=1")
    assert isinstance(client, ExtensionMetadataClient)
    assert client.ext_name == "melon-music"


def test_search_goes_to_the_chosen_source(monkeypatch) -> None:
    from SpotiFLAC.api_mixins.search import search_metadata_async

    class _Client:
        async def search_async(self, query, limit=50):
            return {
                "tracks": [_track("밤편지")],
                "albums": [],
                "artists": [],
                "playlists": [],
            }

    monkeypatch.setattr(
        ExtensionMetadataClient,
        "for_source",
        classmethod(lambda cls, name, manager=None: _Client()),
    )
    shaped = asyncio.run(search_metadata_async("아이유", source="melon-music"))
    assert shaped["tracks"][0]["title"] == "밤편지"
    assert shaped["tracks"][0]["provider"] == "melon-music"


# ---------------------------------------------------------------------------
# The bridge, as extensions written for the mobile app use it
# ---------------------------------------------------------------------------


class _EchoHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's naming
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(
            {"ua": self.headers.get("User-Agent"), "odd": self.headers.get("headers")}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


PROBE = """
registerExtension({
  initialize: function () {},
  get: function (url, second) {
    var r = http.get(url, second);
    return { ok: r.ok, status: r.status, statusCode: r.statusCode, body: r.body };
  }
});
"""


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("needs Node to exercise the real bridge")
    from SpotiFLAC.extensions.runtime import JSRuntime

    # The local server lives on loopback, which the network guard refuses.
    monkeypatch.setenv("SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK", "1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ext = tmp_path / "index.js"
    ext.write_text(PROBE)
    rt = JSRuntime(ext_path=ext)
    rt.start()
    try:
        yield rt, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        rt.stop()
        server.shutdown()


def test_a_response_says_ok_like_the_mobile_runtime(bridge) -> None:
    rt, base = bridge
    good = rt.call("get", base + "/", {})
    assert (good["ok"], good["status"], good["statusCode"]) == (True, 200, 200)
    missing = rt.call("get", base + "/missing", {})
    assert (missing["ok"], missing["status"]) == (False, 404)


def test_headers_wrapped_in_an_options_object_are_sent_as_headers(bridge) -> None:
    rt, base = bridge
    echoed = json.loads(
        rt.call("get", base + "/", {"headers": {"User-Agent": "K-Music"}})["body"]
    )
    assert echoed == {"ua": "K-Music", "odd": None}


def test_a_request_without_a_user_agent_gets_one(bridge) -> None:
    rt, base = bridge
    assert json.loads(rt.call("get", base + "/", {})["body"])["ua"]
    flat = json.loads(rt.call("get", base + "/", {"User-Agent": "flat"})["body"])
    assert flat["ua"] == "flat"


# ---------------------------------------------------------------------------
# What the extensions scrape along with the data (seen on the live sites)
# ---------------------------------------------------------------------------

from SpotiFLAC.core.extension_metadata import clean_album, clean_title  # noqa: E402


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("TITLE 사랑은 늘 도망가", "사랑은 늘 도망가"),  # Genie's title-track badge
        ("Lucky you 곡 선택", "Lucky you"),  # Melon's checkbox label
        ("LOVE ATTACK 좋아요", "LOVE ATTACK"),  # Melon's like button
        ("Title Track Of My Life", "Title Track Of My Life"),  # a real word stays
        (  # Bugs' notice, word for word
            "안내 음악을 재생할 플레이어를 선택해 주세요. 벅스 웹상단 > 플레이어 "
            "선택에서 플레이어를 변경할 수 있습니다.",
            "",
        ),
        ("Title", ""),  # Melon's badge with the name missing
        ("x" * 200, ""),
    ],
)
def test_page_furniture_is_stripped_from_titles(raw, clean) -> None:
    assert clean_title(raw) == clean


@pytest.mark.parametrize(
    "raw, artists, clean",
    [
        (
            "신사와 아가씨 OST Part.2 / 임영웅 - genie",
            "임영웅",
            "신사와 아가씨 OST Part.2",
        ),
        ("SCENEDROME - RESCENE (리센느)", "RESCENE (리센느)", "SCENEDROME"),
        ("[타이틀곡]", "aespa", ""),
        ("Melon", "RESCENE", ""),
        ("Dreams Come True - Remixes", "aespa", "Dreams Come True - Remixes"),
    ],
)
def test_page_titles_are_reduced_to_the_album_name(raw, artists, clean) -> None:
    assert clean_album(raw, artists) == clean


def test_an_instrumental_does_not_take_the_original_s_isrc() -> None:
    spotify = _Spotify([_candidate("사랑은 늘 도망가", "Lim Young Woong", 235909)])
    [inst] = asyncio.run(
        attach_isrcs(
            [_track("사랑은 늘 도망가 (Inst.)", "임영웅", 235909)],
            spotify=spotify,
            isrc_helper=_Isrc(),
        )
    )
    assert inst.isrc == ""


def test_with_no_artist_an_unknown_length_is_not_enough() -> None:
    spotify = _Spotify([_candidate("LOVE ATTACK", "Someone Else")])
    [track] = asyncio.run(
        attach_isrcs(
            [_track("LOVE ATTACK", artists="")], spotify=spotify, isrc_helper=_Isrc()
        )
    )
    assert track.isrc == ""


def test_a_track_link_is_read_from_its_album_page() -> None:
    """Bugs' track page is unreadable; its album page lists the same track."""
    page = {
        "success": True,
        "track": {
            "id": "6507286",
            "name": "안내 음악을 재생할 플레이어를 선택해 주세요. " * 5,
            "artists": "",
            "album_id": "4154942",
            "album_name": "SYNK : COMPLaeXITY - 2026 Special Digital Single",
        },
    }
    album = {
        "id": "4154942",
        "name": "안내",
        "artists": "aespa",
        "tracks": [
            {"id": "6507285", "name": "16 Bit (KARINA Solo)", "artists": "aespa"},
            {
                "id": "6507286",
                "name": "Saddle Up (WINTER Solo)",
                "artists": "aespa",
                "album_name": "[타이틀곡]",
            },
        ],
    }
    bugs = next(s for s in SITES if s.key == "bugs")

    class _Provider:
        def _call(self, method, *args):
            return {"getTrack": page, "getAlbum": album}[method]

    client = ExtensionMetadataClient(
        "bugs-music", bugs, provider_factory=lambda name: _Provider(), match_isrcs=False
    )
    _name, [track], _cover = asyncio.run(
        client.get_url_async("https://music.bugs.co.kr/track/6507286")
    )
    assert (track.title, track.artists, track.track_number) == (
        "Saddle Up (WINTER Solo)",
        "aespa",
        2,
    )
    assert track.album == "SYNK : COMPLaeXITY - 2026 Special Digital Single"


def test_an_album_that_does_not_contain_the_track_is_not_believed() -> None:
    """Melon's extension reports the song's own id as its album id."""
    page = {
        "success": True,
        "track": {"id": "1", "name": "Real Song", "artists": "A", "album_id": "99"},
    }
    other_album = {
        "id": "99",
        "name": "Other",
        "tracks": [{"id": "2", "name": "Wrong"}],
    }

    class _Provider:
        def _call(self, method, *args):
            return {"getTrack": page, "getAlbum": other_album}[method]

    client = ExtensionMetadataClient(
        "melon-music",
        MELON,
        provider_factory=lambda name: _Provider(),
        match_isrcs=False,
    )
    _name, [track], _cover = asyncio.run(
        client.get_url_async("https://www.melon.com/song/detail.htm?songId=1")
    )
    assert track.title == "Real Song"


def test_melon_s_page_title_gives_the_artist_it_left_out() -> None:
    track = to_track(
        {
            "id": "37928381",
            "name": "LOVE ATTACK 좋아요",
            "artists": "",
            "album_name": "LOVE ATTACK - RESCENE (리센느)",
        },
        MELON,
    )
    assert (track.title, track.artists) == ("LOVE ATTACK", "RESCENE (리센느)")
    assert track.album != "LOVE ATTACK - RESCENE (리센느)"


def test_a_title_track_listed_as_its_badge_is_named_from_its_page() -> None:
    album = {
        "id": "11575849",
        "name": "SCENEDROME - RESCENE (리센느)",
        "artists": "RESCENE (리센느)",
        "tracks": [
            {
                "id": "37928380",
                "name": "Lucky you 곡 선택",
                "artists": "RESCENE (리센느)",
            },
            {"id": "37928381", "name": "Title", "artists": "RESCENE (리센느)"},
        ],
    }
    calls = []

    class _Provider:
        def _call(self, method, *args):
            calls.append((method, args))
            if method == "getAlbum":
                return album
            return {
                "success": True,
                "track": {"id": args[0], "name": "LOVE ATTACK 좋아요"},
            }

    client = ExtensionMetadataClient(
        "melon-music",
        MELON,
        provider_factory=lambda name: _Provider(),
        match_isrcs=False,
    )
    name, tracks, _cover = asyncio.run(
        client.get_url_async("https://www.melon.com/album/detail.htm?albumId=11575849")
    )
    assert name == "SCENEDROME"
    assert [(t.track_number, t.title) for t in tracks] == [
        (1, "Lucky you"),
        (2, "LOVE ATTACK"),
    ]
    # Only the track that needed it cost a page.
    assert calls.count(("getTrack", ("37928381",))) == 1
    assert len(calls) == 2

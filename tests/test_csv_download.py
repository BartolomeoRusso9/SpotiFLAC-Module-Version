"""`--csv FILE DEST` — the CSV run itself (see SpotiflacDownloader.run_csv_async).

A CSV is treated as one playlist that happens to live on disk, so the run
inherits the merged-playlist behaviour: a track listed twice is fetched once,
a track already in the destination is not fetched at all, and an M3U named
after the file is written next to them. These check that wiring — the
network-facing halves (catalogue lookups, providers) are replaced, since what
is under test is the plan, not the download.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core import csv_source
from SpotiFLAC.core.models import TrackMetadata
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader


def _track(track_id: str, title: str, artist: str = "Foo Fighters") -> TrackMetadata:
    return TrackMetadata(
        id=track_id,
        title=title,
        artists=artist,
        album="The Colour and the Shape",
        album_artist=artist,
        isrc=f"US{track_id.upper()[:3]}9600011"[:12],
        external_url=f"https://open.spotify.com/track/{track_id}",
    )


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    """A downloader whose catalogue and providers are replaced by fakes."""
    opts = DownloadOptions(
        output_dir=str(tmp_path / "music"),
        services=["ext:none"],
        enrich_metadata=False,
        embed_lyrics=False,
    )
    instance = SpotiflacDownloader(opts)

    catalogue = {
        "https://open.spotify.com/track/aaa": _track("aaa", "Everlong"),
        "https://open.spotify.com/track/bbb": _track("bbb", "Monkey Wrench"),
    }

    async def _resolve_metadata_async(url: str):
        track = catalogue.get(url)
        if track is None:
            return "", [], {}
        return track.title, [track], {"type": "track"}

    # ISRC resolution and the download worker are the two things that would
    # reach the network; the plan is what these tests are about.
    async def _resolve_isrc_bulk_async(tracks):
        return list(tracks)

    downloaded: list[str] = []

    async def _download_pending_async(plan, opts_):
        downloaded.extend(planned.track.title for planned in plan.pending)
        return {}

    monkeypatch.setattr(instance, "_resolve_metadata_async", _resolve_metadata_async)
    monkeypatch.setattr(instance, "_resolve_isrc_bulk_async", _resolve_isrc_bulk_async)
    monkeypatch.setattr(instance, "_download_pending_async", _download_pending_async)

    instance.test_downloaded = downloaded  # type: ignore[attr-defined]
    return instance


def _csv(tmp_path, text: str, name: str = "wishlist.csv") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_csv_of_links_becomes_one_run_with_one_playlist_file(
    tmp_path, downloader
) -> None:
    path = _csv(
        tmp_path,
        "https://open.spotify.com/track/aaa\nhttps://open.spotify.com/track/bbb\n",
    )

    summary = asyncio.run(downloader.run_csv_async(path))

    assert summary["rows"] == 2
    assert summary["resolved"] == 2
    assert summary["tracks"] == 2
    assert downloader.test_downloaded == ["Everlong", "Monkey Wrench"]
    # Named after the file, so two CSVs in one folder don't overwrite each
    # other's playlist.
    assert (tmp_path / "music" / "wishlist.m3u8").is_file()


def test_a_track_listed_twice_is_downloaded_once(tmp_path, downloader) -> None:
    path = _csv(
        tmp_path,
        "https://open.spotify.com/track/aaa\n"
        "https://open.spotify.com/track/aaa\n"
        "https://open.spotify.com/track/bbb\n",
    )

    summary = asyncio.run(downloader.run_csv_async(path))

    assert summary["rows"] == 3
    assert summary["tracks"] == 2
    assert downloader.test_downloaded == ["Everlong", "Monkey Wrench"]


def test_a_track_already_in_the_destination_is_not_fetched_again(
    tmp_path, downloader
) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "Everlong - Foo Fighters.flac").write_bytes(b"not really a flac")

    path = _csv(
        tmp_path,
        "https://open.spotify.com/track/aaa\nhttps://open.spotify.com/track/bbb\n",
    )

    summary = asyncio.run(downloader.run_csv_async(path))

    assert summary["already_present"] == 1
    assert downloader.test_downloaded == ["Monkey Wrench"]
    # The file on disk still earns its place in the playlist.
    assert "Everlong" in (music / "wishlist.m3u8").read_text(encoding="utf-8")


def test_rows_that_could_not_be_matched_are_reported_in_the_summary(
    tmp_path, downloader
) -> None:
    path = _csv(tmp_path, "https://open.spotify.com/track/aaa\nISRC,,\n")
    document = csv_source.read_rows(path)
    resolution = csv_source.CsvResolution(
        document=document,
        resolved=(
            csv_source.ResolvedRow(
                row=document.rows[0],
                url="https://open.spotify.com/track/aaa",
                how="link",
            ),
        ),
        unresolved=(
            csv_source.UnresolvedRow(row=document.rows[1], reason="nothing found"),
        ),
    )

    summary = asyncio.run(downloader.run_csv_async(path, resolution=resolution))

    assert summary["downloaded"] == 0  # the fake worker locates nothing
    assert summary["unresolved"] == [
        {
            "line": document.rows[1].line,
            "input": document.rows[1].label,
            "reason": "nothing found",
            "best": "",
            "score": 0.0,
        }
    ]


def test_a_file_whose_rows_all_fail_stops_before_planning(tmp_path, downloader) -> None:
    path = _csv(tmp_path, "https://open.spotify.com/track/unknown\n")

    summary = asyncio.run(downloader.run_csv_async(path))

    # The link resolved (it is a link), but the catalogue has nothing for it.
    assert summary["resolved"] == 1
    assert summary["tracks"] == 0
    assert downloader.test_downloaded == []


# ── The CLI path (launcher) ───────────────────────────────────────────────


@pytest.fixture
def _restore_cli_logger():
    """Undoes what a CLI run does to the "SpotiFLAC" logger.

    `_run_download_async` installs a console handler and sets
    `propagate = False` on it — correct for a real run, and a side effect on
    every later test in the session that reads records from a logger
    underneath it (caplog attaches to the root).
    """
    import logging

    logger = logging.getLogger("SpotiFLAC")
    handlers = list(logger.handlers)
    propagate, level = logger.propagate, logger.level
    yield
    logger.handlers = handlers
    logger.propagate = propagate
    logger.setLevel(level)


def test_the_cli_hands_the_csv_to_the_csv_run(
    tmp_path, monkeypatch, _restore_cli_logger
) -> None:
    """`spotiflac --csv FILE DEST` must reach run_csv_async, not run_async."""
    from SpotiFLAC import launcher

    calls: dict = {}

    class _FakeDownloader:
        def __init__(self, opts):
            calls["output_dir"] = opts.output_dir

        async def run_csv_async(self, path, **kwargs):
            calls["path"] = path
            calls["kwargs"] = kwargs
            return {"rows": 1, "resolved": 1, "unresolved": [], "tracks": 1}

        async def run_async(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("a CSV run must not go through run_async")

    monkeypatch.setattr(launcher, "SpotiflacDownloader", _FakeDownloader)
    path = _csv(tmp_path, "https://open.spotify.com/track/aaa\n")

    asyncio.run(
        launcher._run_download_async(
            "",
            output_dir=str(tmp_path / "music"),
            services=["ext:none"],
            filename_format="{title} - {artist}",
            use_track_numbers=False,
            use_album_track_numbers=False,
            use_artist_subfolders=False,
            use_album_subfolders=False,
            create_playlist_subfolders=True,
            loop=None,
            quality="LOSSLESS",
            first_artist_only=False,
            include_featuring=True,
            log_level=40,
            output_path=None,
            allow_fallback=True,
            embed_lyrics=False,
            lyrics_providers=[],
            enrich_metadata=False,
            enrich_providers=[],
            qobuz_local_api_url=None,
            tidal_custom_api=None,
            track_max_retries=0,
            post_download_action="none",
            post_download_command="",
            timeout_s=None,
            csv_path=path,
            csv_min_score=0.5,
        )
    )

    assert calls["path"] == path
    assert calls["kwargs"]["min_score"] == 0.5
    assert calls["kwargs"]["resolution"].urls == ["https://open.spotify.com/track/aaa"]


def test_the_unmatched_rows_are_written_where_asked(tmp_path, monkeypatch) -> None:
    from SpotiFLAC import launcher
    from SpotiFLAC.core import spotify_metadata

    class _EmptyCatalogue:
        async def search_tracks_async(self, query: str, limit: int = 8) -> list:
            return []

    # resolve_rows builds the real client only when a row needs a search;
    # this replaces it so the search row is answered with "nothing found"
    # instead of reaching Spotify.
    monkeypatch.setattr(spotify_metadata, "SpotifyMetadataClient", _EmptyCatalogue)

    path = _csv(
        tmp_path,
        "url,title,artist\n"
        ",Nothing Like This,Nobody At All\n"
        "https://open.spotify.com/track/aaa,,\n",
    )
    report = tmp_path / "missed.csv"

    resolution = asyncio.run(
        launcher._resolve_csv_async(
            path,
            delimiter=",",
            min_score=0.62,
            concurrency=2,
            unresolved_path=str(report),
        )
    )

    assert [entry.url for entry in resolution.resolved] == [
        "https://open.spotify.com/track/aaa"
    ]
    assert report.is_file()
    written = report.read_text(encoding="utf-8")
    assert "nothing found" in written
    # The report is itself a valid --csv input, so a corrected row can go
    # straight back in.
    assert "Nothing Like This" in written


def test_an_unreadable_csv_stops_the_run(tmp_path) -> None:
    from SpotiFLAC import launcher

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(
            launcher._resolve_csv_async(
                str(tmp_path / "missing.csv"),
                delimiter=None,
                min_score=0.62,
                concurrency=2,
                unresolved_path=None,
            )
        )

    assert exit_info.value.code == 2

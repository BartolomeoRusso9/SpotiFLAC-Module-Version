"""download_one_async() rejecting a provider that fetched the wrong take.

The check has to live here rather than in extensions/provider.py, which is
where the existing track-identity check lives: a `.sflx` Python extension is
loaded by PythonExtensionProvider, whose __new__ returns the extension's own
provider object, so nothing in extensions/provider.py ever runs for it. The
qobuz extension that fetched a ZZang KARAOKE take of "Like Him" is one of
those, and it reached the filesystem with no host check of any kind.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from SpotiFLAC import downloader as dl
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

REQUESTED_ISRC = "USQX92405794"
KARAOKE_ISRC = "QZYD92602058"


def _track() -> TrackMetadata:
    return TrackMetadata(
        id="6jbYpRPTEFl1HFKHk1IC0m",
        title="Like Him (feat. Lola Young)",
        artists="Tyler, The Creator, Lola Young",
        artist_names=["Tyler, The Creator", "Lola Young"],
        album="CHROMAKOPIA",
        album_artist="Tyler, The Creator",
        isrc=REQUESTED_ISRC,
        duration_ms=278014,
        is_explicit=True,
        cover_url="https://example.invalid/right.jpg",
    )


class _Provider:
    """A provider that resolves `isrc` and writes it back, as they all do."""

    def __init__(self, name: str, isrc: str, tmp_path: Path) -> None:
        self.name = name
        self._isrc = isrc
        self._dir = tmp_path

    def set_progress_callback(self, cb) -> None:
        pass

    async def download_track_async(self, metadata, output_dir, **kwargs):
        metadata.isrc = self._isrc
        metadata.cover_url = f"https://example.invalid/{self.name}.jpg"
        path = self._dir / f"{self.name}.flac"
        path.write_bytes(b"audio")
        return DownloadResult.ok(self.name, str(path))


class _FailsAfterResolving(_Provider):
    """A provider that writes back the ISRC it found, then fails anyway.

    Exactly what the tidal extension does: it resolves the ISRC through
    Qobuz ("[tidal] ISRC from Qobuz (preferred)") and stores it on the
    metadata before discovering it has no API configured to download from.
    """

    async def download_track_async(self, metadata, output_dir, **kwargs):
        metadata.isrc = self._isrc
        return DownloadResult.fail(self.name, "no Tidal APIs configured")


@pytest.fixture
def _no_network(monkeypatch):
    """Deezer's answer for the karaoke ISRC, without asking Deezer."""

    async def fake_lookup(isrc):
        if isrc == KARAOKE_ISRC:
            return {
                "title": "Like Him (Feat. Lola Young) (Melody Karaoke Version)",
                "artist": "ZZang KARAOKE",
                "duration_ms": 279000,
                "explicit": False,
            }
        return None

    monkeypatch.setattr(
        "SpotiFLAC.core.recording_guard._lookup_isrc_async", fake_lookup
    )
    monkeypatch.setattr(dl, "_schedule_hires_check", lambda *a, **k: None)


def _run(providers, tmp_path, track):
    opts = dl.DownloadOptions(output_dir=str(tmp_path), embed_lyrics=False)
    return asyncio.run(
        dl.download_one_async(track, str(tmp_path), providers, opts),
    )


def test_a_karaoke_take_is_refused_and_its_file_deleted(_no_network, tmp_path) -> None:
    track = _track()
    provider = _Provider("qobuz", KARAOKE_ISRC, tmp_path)

    result = _run([provider], tmp_path, track)

    assert not result.success
    assert "Wrong recording" in (result.error or "")
    assert not (tmp_path / "qobuz.flac").exists()


def test_the_next_provider_starts_from_what_was_asked_for(
    _no_network, tmp_path
) -> None:
    """The rejected provider had already written its own ISRC and cover onto
    the shared metadata object. Left there, its ISRC becomes what the guard
    compares against, so the second provider would be judged against the
    karaoke take instead of the track.
    """
    track = _track()
    providers = [
        _Provider("qobuz", KARAOKE_ISRC, tmp_path),
        _Provider("tidal", REQUESTED_ISRC, tmp_path),
    ]

    result = _run(providers, tmp_path, track)

    assert result.success
    assert result.provider == "tidal"
    assert track.isrc == REQUESTED_ISRC
    assert (tmp_path / "tidal.flac").exists()


def test_a_provider_that_agrees_is_left_alone(_no_network, tmp_path) -> None:
    track = _track()
    result = _run([_Provider("tidal", REQUESTED_ISRC, tmp_path)], tmp_path, track)

    assert result.success
    assert (tmp_path / "tidal.flac").exists()


def test_a_failed_provider_does_not_redefine_the_request(_no_network, tmp_path) -> None:
    """The second time the karaoke got through, and the reason it did.

    tidal resolved the karaoke ISRC, wrote it onto the shared metadata and
    failed. qobuz then resolved that same karaoke ISRC — and against a
    request that now *said* karaoke, there was no drift left to notice. The
    request has to survive a provider that delivered nothing.
    """
    track = _track()
    providers = [
        _FailsAfterResolving("tidal", KARAOKE_ISRC, tmp_path),
        _Provider("qobuz", KARAOKE_ISRC, tmp_path),
    ]

    result = _run(providers, tmp_path, track)

    assert not result.success
    assert "Wrong recording" in (result.error or "")
    assert not (tmp_path / "qobuz.flac").exists()
    assert track.isrc == REQUESTED_ISRC


def test_a_failed_provider_may_still_fill_in_a_blank(_no_network, tmp_path) -> None:
    """Only what the request actually said is put back. Nobody knew this
    track's ISRC, so the one tidal found is a gift to the next provider, not
    a corruption of the brief.
    """
    track = _track()
    track.isrc = ""
    providers = [
        _FailsAfterResolving("tidal", REQUESTED_ISRC, tmp_path),
        _Provider("qobuz", REQUESTED_ISRC, tmp_path),
    ]

    result = _run(providers, tmp_path, track)

    assert result.success
    assert result.provider == "qobuz"
    assert track.isrc == REQUESTED_ISRC

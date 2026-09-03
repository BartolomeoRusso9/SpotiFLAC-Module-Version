"""The GUI must hand the client every folder-layout option it exposes.

client.SpotiFLAC defaults create_playlist_subfolders to False while the CLI
and the interactive picker default it to True, so an option the GUI simply
never passes does not fall back to the product default — it silently flips.
That is what happened to playlist subfolders: the settings panel had no
toggle, _download_task never sent the key, and every playlist landed loose
in the download folder.
"""

from __future__ import annotations

import pytest

import SpotiFLAC as spotiflac_pkg
from SpotiFLAC.app import SpotiFLAC_API


class _FakeTrack:
    id = "track-id"
    title = "Title"
    external_url = "https://open.spotify.com/track/track-id"


@pytest.fixture
def captured_options(tmp_path, monkeypatch):
    """Runs _download_task and returns the kwargs the download wrapper got."""
    seen: list[dict] = []

    def _fake_spotiflac(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", _fake_spotiflac)

    def _run(config: dict) -> dict:
        api = SpotiFLAC_API()
        api.download_dir = str(tmp_path)
        api.current_tracks = [_FakeTrack()]
        api.current_url = _FakeTrack.external_url
        api._download_task([0], {"services": ["tidal"], **config})
        assert seen, "_download_task never reached the download call"
        return seen[-1]

    return _run


def test_playlist_subfolders_default_on(captured_options) -> None:
    # A config from a client that predates the toggle must still behave the
    # way the rest of the app does.
    assert captured_options({})["create_playlist_subfolders"] is True


@pytest.mark.parametrize("enabled", [True, False])
def test_playlist_subfolders_follow_the_toggle(captured_options, enabled) -> None:
    opts = captured_options({"create_playlist_subfolders": enabled})
    assert opts["create_playlist_subfolders"] is enabled

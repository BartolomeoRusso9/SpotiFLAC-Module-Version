"""The GUI/web bridge must not let its caller choose a shell command.

Every method on SpotiFLAC_API is reachable two ways: bound wholesale as
pywebview's `js_api` in desktop mode (so any JS running in the window can
call it), and — in `--web` mode — as a POST /api/<method> endpoint, where
the "caller" is whoever can reach the port and `config` is just a JSON
body. `post_download_action="command"` ends up in
downloader._execute_post_action_async(), which runs it through a shell, so
taking it from that dict makes the two indistinguishable.

The CLI path is deliberately not covered here: it never goes through this
class (launcher.py builds client.SpotiFLAC directly), and someone who can
already type a shell command into their own shell gains nothing by typing
it into ours.
"""

from __future__ import annotations

import pytest

import SpotiFLAC as spotiflac_pkg
from SpotiFLAC.app import POST_COMMAND_ENV, SpotiFLAC_API


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

    # _download_task does `from . import SpotiFLAC` at call time, so the
    # name to patch lives on the package, not on SpotiFLAC.app.
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


def test_command_action_from_the_bridge_is_dropped_by_default(
    captured_options, monkeypatch
) -> None:
    monkeypatch.delenv(POST_COMMAND_ENV, raising=False)

    opts = captured_options(
        {
            "post_download_action": "command",
            "post_download_command": "touch /tmp/spotiflac-pwned",
        }
    )

    assert opts["post_download_action"] == "none"
    assert opts["post_download_command"] == ""


def test_command_action_passes_through_once_the_operator_opts_in(
    captured_options, monkeypatch
) -> None:
    monkeypatch.setenv(POST_COMMAND_ENV, "1")

    opts = captured_options(
        {
            "post_download_action": "command",
            "post_download_command": "echo done",
        }
    )

    assert opts["post_download_action"] == "command"
    assert opts["post_download_command"] == "echo done"


@pytest.mark.parametrize("action", ["open_folder", "notify", "none"])
def test_harmless_post_actions_are_never_gated(
    captured_options, monkeypatch, action
) -> None:
    """Only the shell action needs the opt-in; the other two must keep
    working out of the box or the gate has broken an ordinary feature.
    """
    monkeypatch.delenv(POST_COMMAND_ENV, raising=False)
    opts = captured_options({"post_download_action": action})
    assert opts["post_download_action"] == action

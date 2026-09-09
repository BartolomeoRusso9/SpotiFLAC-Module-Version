"""Hi-Res verification reaching the download from the GUI settings panel.

The desktop window and --web mode share one settings dict and one entry
point (SpotiFLAC_API._run_download_batch), so the two toggles are tested
where every frontend meets the client: the kwargs the wrapper is called
with. The markup checks below are what stops a renamed input id from
silently turning the toggles into settings that save and do nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import SpotiFLAC as spotiflac_pkg
from SpotiFLAC.app import SpotiFLAC_API

FRONTEND = Path(spotiflac_pkg.__file__).parent / "frontend"


class _FakeTrack:
    id = "track-0"
    title = "Title"
    external_url = "https://open.spotify.com/track/track-0"


@pytest.fixture()
def download_kwargs(tmp_path, monkeypatch):
    """Runs one GUI download and returns the kwargs the client received."""
    seen: list[dict] = []

    def _fake_spotiflac(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(spotiflac_pkg, "SpotiFLAC", _fake_spotiflac)

    def _run(config: dict | None = None) -> dict:
        api = SpotiFLAC_API()
        api.download_dir = str(tmp_path)
        api.current_tracks = [_FakeTrack()]
        api.current_url = ""
        api._download_task([0], {"services": ["tidal"], **(config or {})})
        assert seen, "_download_task never reached the download call"
        return seen[-1]

    return _run


def test_the_toggles_are_off_unless_the_panel_says_otherwise(
    download_kwargs,
) -> None:
    kwargs = download_kwargs()
    assert kwargs["verify_hires"] is False
    assert kwargs["redownload_fake_hires"] is False


def test_the_check_can_be_enabled_on_its_own(download_kwargs) -> None:
    kwargs = download_kwargs({"verify_hires": True})
    assert kwargs["verify_hires"] is True
    assert kwargs["redownload_fake_hires"] is False


def test_asking_for_the_replacement_turns_the_check_on(download_kwargs) -> None:
    """A settings file carrying only the second toggle is not a no-op."""
    kwargs = download_kwargs({"redownload_fake_hires": True})
    assert kwargs["verify_hires"] is True
    assert kwargs["redownload_fake_hires"] is True


def test_the_settings_panel_offers_both_toggles() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'id="config-verify-hires"' in html
    assert 'id="config-redownload-fake-hires"' in html
    # The replace row is nested under the check and revealed by it.
    assert 'onchange="onVerifyHiresChange()"' in html
    assert "verify-hires-opt" in html


def test_turning_the_check_off_clears_the_toggle_it_hides() -> None:
    """Found in the browser, not in a test: the nested toggle stayed checked
    behind a hidden row after the check above it was switched off, and the
    backend reads "replace" as implying "verify" — so the setting the user
    had just turned off came straight back on at the next download.
    """
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    handler = js[js.index("function onVerifyHiresChange()") :]
    handler = handler[: handler.index("\n}")]
    assert "$('config-redownload-fake-hires').checked = false" in handler


def test_a_config_carrying_only_the_nested_key_loads_visibly() -> None:
    """The other half of the fix. Clearing the hidden toggle would silently
    drop `redownload_fake_hires: true` out of a settings file that did not
    also carry `verify_hires`, so the panel mirrors the backend's implication
    on load instead.
    """
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert (
        "$('config-verify-hires').checked = "
        "!!cfg.verify_hires || !!cfg.redownload_fake_hires" in js
    )


def test_the_frontend_round_trips_both_keys() -> None:
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    for key in ("verify_hires", "redownload_fake_hires"):
        # Written into the config the backend reads...
        assert f"{key}:" in js
    # ...and read back into the panel when the settings are reloaded.
    assert "$('config-verify-hires').checked = !!cfg.verify_hires" in js
    assert (
        "$('config-redownload-fake-hires').checked = !!cfg.redownload_fake_hires" in js
    )
    assert "function onVerifyHiresChange()" in js

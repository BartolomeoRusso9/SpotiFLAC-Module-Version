import argparse
import json
import logging

import pytest

from SpotiFLAC.core.profiles import ProfileConfig
from SpotiFLAC.launcher import (
    _resolve_log_level,
    _split_positionals,
    load_config,
    parse_args,
)


def test_default_level_reports_run_milestones():
    # Was ERROR, which hid the info lines that say what a run is doing —
    # the provider being tried, the ticket, the audio fetch, the transcode —
    # so a stalled download printed nothing until it gave up. The libraries
    # that would flood INFO are pinned separately by _quiet_noisy_libraries().
    assert _resolve_log_level(verbose=False) == logging.INFO
    assert _resolve_log_level(verbose=True) == logging.DEBUG


def test_a_quieter_level_is_still_reachable():
    assert _resolve_log_level(verbose=False, explicit="ERROR") == logging.ERROR
    assert _resolve_log_level(verbose=False, explicit="WARNING") == logging.WARNING


def test_profile_config_accepts_named_log_levels():
    cfg = ProfileConfig.model_validate({"log_level": "warn"})
    assert cfg.log_level == logging.WARNING

    cfg = ProfileConfig.model_validate({"log_level": "ERR"})
    assert cfg.log_level == logging.ERROR


def test_profile_config_rejects_invalid_level():
    try:
        ProfileConfig.model_validate({"log_level": "not-a-level"})
    except ValueError:
        return
    raise AssertionError("Expected ValueError for invalid log_level")


def test_load_config_returns_empty_dict_for_invalid_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("{broken json", encoding="utf-8")

    assert load_config() == {}


def test_parse_args_uses_profile_defaults(monkeypatch):
    defaults = {
        "services": ["ext:tidal-web"],
        "m3u_format": "m3u",
        "quality": "HI_RES_LOSSLESS",
        "verbose": True,
    }
    monkeypatch.setattr(
        "sys.argv",
        [
            "spotiflac",
            "--quality",
            "HI_RES_LOSSLESS",
            "--m3u",
            "m3u",
        ],
    )

    args = parse_args(profile_defaults=defaults)

    assert args.quality == "HI_RES_LOSSLESS"
    assert args.verbose is True
    assert args.m3u_format == "m3u"
    assert args.service == ["ext:tidal-web"]


def test_split_positionals_handles_playlist_url_mode():
    args = argparse.Namespace(
        playlists=["https://example.com/list1", "https://example.com/list2"],
        url="https://example.com/track",
        output_dir="/tmp/out",
    )

    playlists, output_dir = _split_positionals(args)

    assert playlists == [
        "https://example.com/track",
        "https://example.com/list1",
        "https://example.com/list2",
    ]
    assert output_dir == "/tmp/out"


def test_load_config_validates_profile_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Place values at root instead of under "default", with non-default values
    payload = {
        "services": ["qobuz"],
        "quality": "HI_RES",
        "embed_lyrics": False,
    }
    (tmp_path / "config.json").write_text(json.dumps(payload), encoding="utf-8")

    cfg = load_config()

    # Verify load_config reads the file rather than returning defaults
    assert cfg["services"] == ["qobuz"]
    assert cfg["quality"] == "HI_RES"
    assert cfg["embed_lyrics"] is False


def test_js_extension_provider_initializes_base_validation_cache(monkeypatch, tmp_path):
    from SpotiFLAC.extensions.provider import JSExtensionProvider

    class DummyExtension:
        name = "tidal-web"
        index_js = str(tmp_path / "index.js")
        manifest = {}

        def default_settings(self):
            return {}

    class DummyManager:
        def __init__(self, ext_dir=None):
            self.ext_dir = ext_dir

        def get_installed(self, ext_id):
            assert ext_id == "tidal-web"
            return DummyExtension()

        def load_settings(self, ext_id):
            assert ext_id == "tidal-web"
            return {}

    monkeypatch.setattr("SpotiFLAC.extensions.provider.ExtensionManager", DummyManager)
    monkeypatch.setattr(
        "SpotiFLAC.extensions.provider.signed_session_client", lambda manifest: None
    )

    provider = JSExtensionProvider("tidal-web")

    assert hasattr(provider, "_validated_flac_files")
    assert isinstance(provider._validated_flac_files, dict)


def test_service_options_are_deduplicated_from_installed_extensions(
    monkeypatch,
):
    from SpotiFLAC.extensions.catalog import installed_service_ids

    class DummyExt:
        def __init__(self, name, is_download_provider=True):
            self.name = name
            self.is_download_provider = is_download_provider
            self.manifest = {"type": ["download_provider"]}

    class DummyManager:
        def __init__(self, auto_install_downloads=False):
            self.auto_install_downloads = auto_install_downloads

        def list_installed(self):
            return [
                DummyExt("tidal-web"),
                DummyExt("tidal-py"),
                DummyExt("qobuz-web"),
                DummyExt("soundcloud"),
            ]

    # Patched where the discovery actually lives: every menu — the TUI, the
    # GUI's Settings list, and the wizard while it lasts — reads
    # extensions/catalog.installed_download_services().
    monkeypatch.setattr("SpotiFLAC.extensions.catalog.ExtensionManager", DummyManager)

    assert installed_service_ids() == ["qobuz", "soundcloud", "tidal"]


def test_the_tui_says_so_when_no_download_provider_is_installed(monkeypatch):
    """An empty provider list is a setup step, not a wrong choice.

    The wizard used to refuse to start and print where to fix it. The TUI
    cannot refuse to open — you configure the registry from inside it — so it
    says the same thing in the two places you would look: beside the empty
    provider list, and in the readiness banner.
    """
    import asyncio

    class DummyManager:
        def __init__(self, auto_install_downloads=False):
            self.auto_install_downloads = auto_install_downloads

        def list_installed(self):
            return []

    monkeypatch.setattr("SpotiFLAC.extensions.catalog.ExtensionManager", DummyManager)

    from SpotiFLAC.tui.app import SpotiFLACTui
    from SpotiFLAC.tui.config_state import ConfigState

    async def _shown():
        state = ConfigState(url="https://open.spotify.com/track/x", output_dir="/tmp/o")
        async with SpotiFLACTui(state).run_test() as pilot:
            for _ in range(4):
                await pilot.pause()
            notice = pilot.app.query_one("#no-providers")
            banner = pilot.app.query_one("#config-problems")
            return notice.display, str(notice.render()), str(banner.render())

    displayed, notice, banner = asyncio.run(_shown())
    assert displayed is True
    assert "No download provider is installed" in notice
    assert "Extensions panel" in notice
    assert "registry is configured" in banner

import argparse
import json
import logging

from SpotiFLAC.core.profiles import ProfileConfig
from SpotiFLAC.launcher import _split_positionals, load_config, parse_args


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
    payload = {
        "default": {
            "services": ["tidal"],
            "quality": "LOSSLESS",
            "embed_lyrics": True,
        }
    }
    (tmp_path / "config.json").write_text(json.dumps(payload), encoding="utf-8")

    cfg = load_config()

    assert cfg["services"] == ["tidal"]
    assert cfg["quality"] == "LOSSLESS"
    assert cfg["embed_lyrics"] is True

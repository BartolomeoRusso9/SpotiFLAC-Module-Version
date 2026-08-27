import asyncio
import json

from SpotiFLAC.core import profiles


def test_save_and_list_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_PROFILES_FILE", tmp_path / "profiles.json")

    async def _run():
        await profiles.save_profile_async(
            "alpha",
            {
                "services": ["tidal"],
                "quality": "LOSSLESS",
                "output_path": "/tmp/ignored",
                "url": "https://example.com/track",
            },
        )

        names = await profiles.list_profiles_async()
        assert names == ["alpha"]

        stored = await profiles.get_profile_async("alpha")
        assert stored is not None
        assert stored["services"] == ["tidal"]
        assert stored["quality"] == "LOSSLESS"
        assert "output_path" not in stored
        assert "url" not in stored
        assert profiles._PROFILES_FILE.exists()

        file_data = json.loads(profiles._PROFILES_FILE.read_text(encoding="utf-8"))
        assert file_data["alpha"]["services"] == ["tidal"]

    asyncio.run(_run())


def test_invalid_profile_is_skipped_on_load(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_PROFILES_FILE", tmp_path / "profiles.json")

    async def _run():
        data = {
            "valid": {"services": ["tidal"], "quality": "LOSSLESS"},
            "broken": {"log_level": "not-a-level"},
            "not_a_dict": ["oops"],
        }
        profiles._PROFILES_FILE.write_text(json.dumps(data), encoding="utf-8")

        loaded = await profiles._load_async()
        assert "valid" in loaded
        assert "broken" not in loaded
        assert "not_a_dict" not in loaded

    asyncio.run(_run())


def test_rename_and_delete_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_PROFILES_FILE", tmp_path / "profiles.json")

    async def _run():
        await profiles.save_profile_async(
            "old", {"services": ["tidal"], "quality": "HIGH"}
        )
        assert await profiles.rename_profile_async("old", "new") is True
        assert await profiles.get_profile_async("old") is None
        assert await profiles.get_profile_async("new") is not None

        assert await profiles.delete_profile_async("new") is True
        assert await profiles.list_profiles_async() == []

    asyncio.run(_run())


def test_delete_missing_profile_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_PROFILES_FILE", tmp_path / "profiles.json")

    async def _run():
        assert await profiles.delete_profile_async("missing") is False

    asyncio.run(_run())


def test_watch_round_trips_through_a_saved_profile(tmp_path, monkeypatch):
    """Regression test: ProfileConfig's `extra: ignore` means a field with
    no matching attribute on the model is silently dropped, not saved —
    `watch` was added to --save-profile's dict before it existed on
    ProfileConfig, which would have made it vanish on every save.
    """
    monkeypatch.setattr(profiles, "_PROFILES_FILE", tmp_path / "profiles.json")

    async def _run():
        await profiles.save_profile_async(
            "watching", {"services": ["tidal"], "watch": 60}
        )
        stored = await profiles.get_profile_async("watching")
        assert stored is not None
        assert stored["watch"] == 60

    asyncio.run(_run())


def test_profile_config_model_accepts_known_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_PROFILES_FILE", tmp_path / "profiles.json")

    cfg = profiles.ProfileConfig.model_validate({"log_level": "warning"})
    assert cfg.log_level == 30

    cfg2 = profiles.ProfileConfig.model_validate({"log_level": "error"})
    assert cfg2.log_level == 40

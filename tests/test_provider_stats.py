import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from SpotiFLAC.core import provider_stats
from SpotiFLAC.core.provider_stats import ProviderScorer


class ProviderStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.environ_patcher = patch.dict(
            os.environ,
            {"XDG_CACHE_HOME": self.tempdir.name},
        )
        self.environ_patcher.start()
        ProviderScorer._instance = None

    def tearDown(self) -> None:
        self.environ_patcher.stop()
        ProviderScorer._instance = None
        self.tempdir.cleanup()

    def test_cache_path_uses_xdg_cache_home(self) -> None:
        cache_path = provider_stats.get_cache_path()
        assert str(cache_path).startswith(self.tempdir.name)
        assert cache_path.name.endswith("provider_priority.json")

    def test_record_success_and_prioritize(self) -> None:
        scorer = ProviderScorer()
        asyncio.run(scorer.reset_async())

        asyncio.run(scorer.record_failure_async("test", "http://api.example.com/bad"))
        asyncio.run(scorer.record_success_async("test", "http://api.example.com/good"))

        ordering = asyncio.run(
            scorer.prioritize_async(
                "test",
                [
                    "http://api.example.com/bad",
                    "http://api.example.com/good",
                    "http://api.example.com/new",
                ],
            ),
        )
        assert ordering[0] == "http://api.example.com/good"
        assert "http://api.example.com/new" in ordering

    def test_persistence_survives_new_instance(self) -> None:
        scorer = ProviderScorer()
        asyncio.run(scorer.reset_async())
        asyncio.run(scorer.record_success_async("test", "http://api.example.com/good"))

        ProviderScorer._instance = None
        new_scorer = ProviderScorer()
        ordering = asyncio.run(
            new_scorer.prioritize_async(
                "test",
                ["http://api.example.com/good", "http://api.example.com/bad"],
            ),
        )
        assert ordering[0] == "http://api.example.com/good"


if __name__ == "__main__":
    unittest.main()

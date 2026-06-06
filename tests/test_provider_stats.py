import os
import tempfile
import unittest
from unittest.mock import patch

from backend.core import provider_stats
from backend.core.provider_stats import ProviderScorer


class ProviderStatsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.environ_patcher = patch.dict(os.environ, {"XDG_CACHE_HOME": self.tempdir.name})
        self.environ_patcher.start()
        ProviderScorer._instance = None

    def tearDown(self):
        self.environ_patcher.stop()
        ProviderScorer._instance = None
        self.tempdir.cleanup()

    def test_cache_path_uses_xdg_cache_home(self):
        cache_path = provider_stats.get_cache_path()
        self.assertTrue(str(cache_path).startswith(self.tempdir.name))
        self.assertTrue(cache_path.name.endswith("provider_priority.json"))

    def test_record_success_and_prioritize(self):
        scorer = ProviderScorer()
        scorer.reset()

        scorer.record_failure("test", "http://api.example.com/bad")
        scorer.record_success("test", "http://api.example.com/good")

        ordering = scorer.prioritize("test", ["http://api.example.com/bad", "http://api.example.com/good", "http://api.example.com/new"])
        self.assertEqual(ordering[0], "http://api.example.com/good")
        self.assertIn("http://api.example.com/new", ordering)

    def test_persistence_survives_new_instance(self):
        scorer = ProviderScorer()
        scorer.reset()
        scorer.record_success("test", "http://api.example.com/good")

        ProviderScorer._instance = None
        new_scorer = ProviderScorer()
        ordering = new_scorer.prioritize("test", ["http://api.example.com/good", "http://api.example.com/bad"])
        self.assertEqual(ordering[0], "http://api.example.com/good")


if __name__ == "__main__":
    unittest.main()

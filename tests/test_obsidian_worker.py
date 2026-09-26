import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_daily_pipeline.obsidian_worker import WorkerError, WorkerSettings, target_directory, validate_startup_configuration, write_favorite


class ObsidianWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        self.vault.mkdir()
        self.settings = WorkerSettings("https://news.mariespace.cn/api/favorites", "test-token", self.vault, "待读清单")
        self.job = {
            "article_id": "a" * 64,
            "title_en": "English title",
            "title_zh": "中文标题",
            "what_happened_en": "English happened.",
            "what_happened_zh": "中文发生了什么。",
            "why_it_matters_en": "English importance.",
            "why_it_matters_zh": "中文为什么重要。",
            "source": "Official Source",
            "published_at": "2026-09-20T00:00:00+00:00",
            "original_url": "https://example.com/article",
            "lease_id": "lease-1",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_write_is_bilingual_and_idempotent(self):
        destination, created = write_favorite(self.job, self.settings, now=lambda: datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertTrue(created)
        content = destination.read_text(encoding="utf-8")
        self.assertIn("# English title", content)
        self.assertIn("# 中文标题", content)
        self.assertLess(content.index("# English title"), content.index("# 中文标题"))
        self.assertIn("待读清单", str(destination))
        same_destination, created_again = write_favorite(self.job, self.settings)
        self.assertEqual(destination, same_destination)
        self.assertFalse(created_again)

    def test_rejects_destination_escape(self):
        settings = WorkerSettings("https://news.mariespace.cn/api/favorites", "test-token", self.vault, "../outside")
        with self.assertRaises(WorkerError):
            write_favorite(self.job, settings)

    def test_chinese_relative_directory_can_resolve_write_stat_and_read_back(self):
        directory = target_directory(self.settings)
        self.assertEqual(directory, self.vault / "待读清单")
        self.assertFalse(directory.exists())
        destination, created = write_favorite(self.job, self.settings)
        self.assertTrue(created)
        self.assertTrue(directory.is_dir())
        self.assertTrue(destination.is_file())
        self.assertTrue(destination.stat().st_size > 0)
        self.assertIn("English title", destination.read_text(encoding="utf-8"))

    def test_startup_validation_creates_and_probes_chinese_target_directory(self):
        directory = target_directory(self.settings)
        self.assertFalse(directory.exists())
        validate_startup_configuration(self.settings)
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.glob(".mariespace-write-check-*.tmp")), [])

    def test_rejects_windows_illegal_relative_path_before_write(self):
        settings = WorkerSettings("https://news.mariespace.cn/api/favorites", "test-token", self.vault, "07??/待读清单")
        with self.assertRaisesRegex(WorkerError, "Invalid Obsidian favorites path configuration"):
            target_directory(settings)


if __name__ == "__main__":
    unittest.main()

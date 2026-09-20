import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_daily_pipeline.obsidian_worker import WorkerError, WorkerSettings, render_markdown, write_favorite


class ObsidianWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        self.vault.mkdir()
        self.settings = WorkerSettings("https://news.mariespace.cn/api/favorites", "test-token", self.vault, "07资源/待读清单")
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
        self.assertIn("07资源", str(destination))
        same_destination, created_again = write_favorite(self.job, self.settings)
        self.assertEqual(destination, same_destination)
        self.assertFalse(created_again)

    def test_rejects_destination_escape(self):
        settings = WorkerSettings("https://news.mariespace.cn/api/favorites", "test-token", self.vault, "../outside")
        with self.assertRaises(WorkerError):
            write_favorite(self.job, settings)


if __name__ == "__main__":
    unittest.main()

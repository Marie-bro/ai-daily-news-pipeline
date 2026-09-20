from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from ai_daily_pipeline.models import Article
from ai_daily_pipeline.store import ArticleStore


class StoreTests(unittest.TestCase):
    def test_existing_usage_table_gains_raw_usage_column(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, model TEXT NOT NULL,
                created_at TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                prompt_cache_hit_tokens INTEGER, prompt_cache_miss_tokens INTEGER, reasoning_tokens INTEGER)""")
            connection.commit()
            connection.close()
            store = ArticleStore(path)
            try:
                usage = {"total_tokens": 12, "new_vendor_field": {"sample": 1}}
                store.record_usage(task="test", model="configured", created_at="2026-09-14T00:00:00+00:00", usage=usage)
                store.commit()
                self.assertEqual(store.usage_rows()[0]["raw_usage_json"], '{"total_tokens": 12, "new_vendor_field": {"sample": 1}}')
            finally:
                store.close()

    def test_store_rejects_url_and_fingerprint_duplicates(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        article = Article("a", "ai", "Title", "Title", "Official", "official_blog", datetime(2026, 9, 14, tzinfo=UTC).isoformat(), "https://example.com/a", "en", "raw text " * 20, "clean text " * 20, "fingerprint", datetime(2026, 9, 14, tzinfo=UTC).isoformat(), "source_verified")
        with TemporaryDirectory() as directory:
            store = ArticleStore(Path(directory) / "articles.sqlite3")
            try:
                self.assertTrue(store.add(article))
                self.assertFalse(store.add(article))
            finally:
                store.close()

    def test_store_purges_articles_from_blocked_hosts(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        article = Article("a", "ai", "Title", "Title", "Official", "official_blog", datetime(2026, 9, 14, tzinfo=UTC).isoformat(), "https://github.com/example/a", "en", "raw text " * 20, "clean text " * 20, "fingerprint", datetime(2026, 9, 14, tzinfo=UTC).isoformat(), "source_verified")
        with TemporaryDirectory() as directory:
            store = ArticleStore(Path(directory) / "articles.sqlite3")
            try:
                store.add(article)
                self.assertEqual(store.purge_articles_for_hosts(("github.com", "openai.com")), 1)
                self.assertEqual(store.unenriched_articles("task", 8), [])
            finally:
                store.close()

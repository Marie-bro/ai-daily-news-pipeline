from datetime import UTC, datetime
import unittest

from ai_daily_pipeline.models import Article
from ai_daily_pipeline.store import ArticleStore


class StoreTests(unittest.TestCase):
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

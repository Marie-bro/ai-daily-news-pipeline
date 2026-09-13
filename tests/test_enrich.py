from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from ai_daily_pipeline.enrich import EnrichmentError, TASK_NAME, _validated_enrichments, run_enrichment
from ai_daily_pipeline.models import Article
from ai_daily_pipeline.store import ArticleStore


def article() -> Article:
    now = datetime(2026, 9, 14, tzinfo=UTC).isoformat()
    return Article("article-1", "ai", "A release", "A release", "Official", "official_blog", now,
                   "https://example.com/release", "en", "raw " * 60, "The source discusses an AI release. " * 20,
                   "fingerprint-1", now, "source_verified")


def model_item(article_id: str = "article-1") -> dict[str, object]:
    return {
        "id": article_id, "title_cn": "一个发布", "title_original": "ignored", "source": "ignored",
        "published_at": "ignored", "original_url": "https://invalid.example", "key_points_original": ["Point one", "Point two"],
        "translation": ["要点一", "要点二"], "summary_cn": "中文摘要", "summary_en": "English summary",
        "relevance": "Why it matters", "useful_expressions": ["AI release", "source text"],
    }


class EnrichmentTests(unittest.TestCase):
    def test_validation_rejects_unknown_candidate_id(self):
        with self.assertRaises(EnrichmentError):
            _validated_enrichments({"items": [model_item("unknown")]}, [article()], "test-model", "2026-09-14T00:00:00+00:00")

    def test_run_saves_verified_metadata_and_actual_usage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            store.add(article())
            store.close()
            response = json.dumps({"items": [model_item()]}, ensure_ascii=False)
            usage = {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200,
                     "prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 100}
            with patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                client.return_value.complete_json.return_value = (response, usage, "deepseek-test")
                result = run_enrichment(root)
            self.assertEqual(result.saved, 1)
            saved = json.loads((root / "data" / "latest-enrichment.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"][0]["original_url"], "https://example.com/release")
            self.assertEqual(saved["usage"]["total_tokens"], 200)
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            try:
                self.assertEqual(len(store.unenriched_articles(TASK_NAME, 8)), 0)
                self.assertEqual(store.usage_rows()[0]["prompt_cache_hit_tokens"], 20)
            finally:
                store.close()

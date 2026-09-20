from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from ai_daily_pipeline.deepseek import DeepSeekError
from ai_daily_pipeline.enrich import EnrichmentError, TASK_NAME, _dedupe_events, _validated_enrichments, run_enrichment
from ai_daily_pipeline.models import Article, Enrichment
from ai_daily_pipeline.store import ArticleStore


def article() -> Article:
    now = datetime(2026, 9, 14, tzinfo=UTC).isoformat()
    return Article("article-1", "ai", "A release", "A release", "Official", "official_blog", now,
                   "https://example.com/release", "en", "raw " * 60, "The source discusses an AI release. " * 20,
                   "fingerprint-1", now, "source_verified")


def model_item(article_id: str = "article-1") -> dict[str, object]:
    return {
        "id": article_id, "category": "ai", "title_cn": "????",
        "title_original": "ignored", "source": "ignored", "published_at": "ignored",
        "original_url": "https://invalid.example", "what_happened": "?????????????????",
        "why_it_matters": "?????????????????", "importance_score": 80,
    }


class EnrichmentTests(unittest.TestCase):
    def test_same_event_from_multiple_sources_occupies_one_daily_item(self):
        base = dict(article_id="a", task=TASK_NAME, generated_at="2026-09-14T00:00:00+00:00", model="m",
                    title_original="Original", source="Official", published_at="2026-09-14T00:00:00+00:00",
                    original_url="https://example.com/a", category="chips", original_language="en",
                    what_happened="发生了事件", why_it_matters="值得关注", importance_score=90)
        first = Enrichment(title_cn="公司发布新一代芯片平台", **base)
        second = Enrichment(title_cn="公司发布新一代芯片平台！", **{**base, "article_id": "b", "source": "Media", "original_url": "https://example.com/b", "importance_score": 80})
        self.assertEqual(_dedupe_events([second, first]), [first])

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
                result = run_enrichment(root, now=datetime(2026, 9, 14, tzinfo=UTC))
            self.assertEqual(result.saved, 1)
            saved = json.loads((root / "data" / "latest-enrichment.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"][0]["original_url"], "https://example.com/release")
            self.assertEqual(saved["items"][0]["original_language"], "en")
            self.assertEqual(saved["usage"]["total_tokens"], 200)
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            try:
                self.assertEqual(len(store.unenriched_articles(TASK_NAME, 8)), 0)
                self.assertEqual(store.usage_rows()[0]["prompt_cache_hit_tokens"], 20)
                self.assertEqual(json.loads(store.usage_rows()[0]["raw_usage_json"]), usage)
            finally:
                store.close()

    def test_rejected_response_still_records_full_raw_usage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            store.add(article())
            store.close()
            usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                     "prompt_tokens_details": {"cached_tokens": 12}}
            response = json.dumps({"items": [model_item("unknown")]})
            with patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                client.return_value.complete_json.return_value = (response, usage, "configured-test-model")
                with self.assertRaises(EnrichmentError):
                    run_enrichment(root, now=datetime(2026, 9, 14, tzinfo=UTC))
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            try:
                row = store.usage_rows()[0]
                self.assertEqual(row["task"], TASK_NAME + "_rejected")
                self.assertEqual(json.loads(row["raw_usage_json"]), usage)
                self.assertEqual(len(store.unenriched_articles(TASK_NAME, 8)), 1)
            finally:
                store.close()

    def test_truncated_response_usage_is_recorded_without_enrichment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            store.add(article())
            store.close()
            usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                     "prompt_tokens_details": {"cached_tokens": 12}}
            with patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                client.return_value.complete_json.side_effect = DeepSeekError(
                    "DeepSeek response was not fully generated", usage=usage, model="configured-test-model"
                )
                with self.assertRaises(DeepSeekError):
                    run_enrichment(root, now=datetime(2026, 9, 14, tzinfo=UTC))
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            try:
                row = store.usage_rows()[0]
                self.assertEqual(row["task"], TASK_NAME + "_rejected")
                self.assertEqual(json.loads(row["raw_usage_json"]), usage)
                self.assertEqual(len(store.unenriched_articles(TASK_NAME, 8)), 1)
            finally:
                store.close()

    def test_old_candidate_does_not_trigger_model_or_overwrite_last_report(self):
        now = datetime(2026, 9, 14, tzinfo=UTC)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            previous = data / "latest-enrichment.json"
            previous.write_text('{"previous": true}', encoding="utf-8")
            store = ArticleStore(data / "ai_daily.sqlite3")
            store.add(replace(article(), published_at="2026-09-01T00:00:00+00:00"))
            store.close()
            with patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                result = run_enrichment(root, now=now)
            client.assert_not_called()
            self.assertEqual(result.candidates, 0)
            self.assertIsNone(result.output_path)
            self.assertEqual(previous.read_text(encoding="utf-8"), '{"previous": true}')

    def test_dry_run_writes_preview_separately(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            previous = data / "latest-enrichment.json"
            previous.write_text('{"previous": true}', encoding="utf-8")
            store = ArticleStore(data / "ai_daily.sqlite3")
            store.add(article())
            store.close()
            with patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                result = run_enrichment(root, dry_run=True, now=datetime(2026, 9, 14, tzinfo=UTC))
            client.assert_not_called()
            self.assertEqual(result.output_path.name, "enrichment-preview.json")
            self.assertEqual(previous.read_text(encoding="utf-8"), '{"previous": true}')

    def test_category_and_compact_schema_are_enforced(self):
        invalid = model_item()
        invalid["category"] = "finance"
        with self.assertRaisesRegex(EnrichmentError, "invalid category"):
            _validated_enrichments({"items": [invalid]}, [article()], "model", article().created_at)
        valid = _validated_enrichments({"items": [model_item()]}, [article()], "model", article().created_at)[0]
        self.assertEqual(valid.category, "ai")
        self.assertNotIn("translation", valid.to_dict())
        self.assertNotIn("summary_en", valid.to_dict())

    def test_generated_blocked_content_is_rejected_before_storage(self):
        item = model_item()
        item["what_happened"] = "OpenAI 发布了未经来源支持的消息"
        with self.assertRaisesRegex(EnrichmentError, "blocked content"):
            _validated_enrichments({"items": [item]}, [article()], "model", article().created_at)

    def test_token_guard_blocks_request_before_client_creation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            store.add(article())
            store.close()
            with patch.dict("os.environ", {"MAX_DAILY_TOKENS": "100"}), patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                with self.assertRaisesRegex(EnrichmentError, "Daily token guard"):
                    run_enrichment(root, now=datetime(2026, 9, 14, tzinfo=UTC))
            client.assert_not_called()

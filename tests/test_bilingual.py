from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_daily_pipeline.bilingual import BilingualValidationError, _dates, _numbers, _times, _versions, normalize_fact_schema, validate_semantic_consistency
from ai_daily_pipeline.enrich import TASK_NAME, run_enrichment
from ai_daily_pipeline.models import Article
from ai_daily_pipeline.store import ArticleStore


def schema() -> dict[str, object]:
    return normalize_fact_schema({
        "article_id": "article-1", "category": "science",
        "core_facts": [
            {
                "id": "institution", "type": "institution", "value": "MIT",
                "rendered_in": ["title", "what_happened"],
                "english_forms": ["MIT", "Massachusetts Institute of Technology"],
                "chinese_forms": ["MIT", "\u9ebb\u7701\u7406\u5de5\u5b66\u9662"],
            },
            {
                "id": "metric", "type": "number", "value": "1.2 billion",
                "rendered_in": ["what_happened"],
                "english_forms": ["1.2 billion"], "chinese_forms": ["12 \u4ebf"],
            },
            {
                "id": "version", "type": "version", "value": "v4.1",
                "rendered_in": ["why_it_matters"],
                "english_forms": ["v4.1"], "chinese_forms": ["v4.1"],
            },
        ],
        "key_entities": ["MIT"], "dates": [], "numbers": ["1.2 billion"], "versions": ["v4.1"],
        "products": [], "companies": [], "technologies": [], "scope": "", "limitations": [],
        "importance_reasons": ["version"],
    }, "article-1")


class SharedFactSchemaTests(unittest.TestCase):
    def test_natural_entity_and_metric_translation_passes(self):
        facts = schema()
        validate_semantic_consistency("MIT reports a result.", "\u9ebb\u7701\u7406\u5de5\u5b66\u9662\u53d1\u5e03\u4e86\u7ed3\u679c\u3002", "title", facts)
        validate_semantic_consistency(
            "MIT reached 1.2 billion.", "\u9ebb\u7701\u7406\u5de5\u5b66\u9662\u8fbe\u5230 12 \u4ebf\u3002", "what_happened", facts,
        )
        validate_semantic_consistency("Version v4.1 matters.", "v4.1 \u7248\u672c\u5f88\u91cd\u8981\u3002", "why_it_matters", facts)

    def test_numeric_conflict_is_hard_fact_failure_with_one_field_repair(self):
        with self.assertRaises(BilingualValidationError) as raised:
            validate_semantic_consistency(
                "MIT reached 1.2 billion.", "\u9ebb\u7701\u7406\u5de5\u5b66\u9662\u8fbe\u5230 11 \u4ebf\u3002", "what_happened", schema(),
            )
        self.assertEqual(raised.exception.stage, "hard_facts")
        self.assertTrue(raised.exception.hard_fact_conflict)
        self.assertEqual(raised.exception.repair_fields, ("what_happened",))

    def test_chinese_numbers_adjacent_to_chinese_text_are_compared(self):
        facts = schema()
        validate_semantic_consistency(
            "MIT reached 1.2 billion.", "\u9ebb\u7701\u7406\u5de5\u5b66\u9662\u8fbe\u523012\u4ebf\u3002", "what_happened", facts,
        )

    def test_two_digit_chinese_date_consumes_the_full_day(self):
        self.assertEqual(_dates("2026\u5e749\u670817\u65e5"), {"2026-09-17"})

    def test_date_ranges_and_times_do_not_be_mistaken_for_metrics(self):
        self.assertEqual(_dates("October 3–17"), {"10-03", "10-17"})
        self.assertEqual(_times("Tickets open at 14:00"), {"14:00"})

    def test_number_normalization_handles_scales_multiples_and_chinese_classifiers(self):
        self.assertEqual(_numbers("1.2 billion", "en"), {"number:1200000000"})
        self.assertEqual(_numbers("12\u4ebf", "zh"), {"number:1200000000"})
        self.assertEqual(_numbers("3.13x", "en"), {"multiple:3.13"})
        self.assertEqual(_numbers("3.13\u500d", "zh"), {"multiple:3.13"})
        self.assertEqual(_numbers("\u4e5d\u5708", "zh"), {"number:9"})
        self.assertEqual(_numbers("more than 2,800", "en"), {"number:2800"})
        self.assertEqual(_numbers("three billion", "en"), {"number:3000000000"})
        self.assertEqual(_numbers("a two-step implementation", "en"), {"number:2"})
        self.assertEqual(_numbers("six-particle amplitudes", "en"), {"number:6"})
        self.assertEqual(_numbers("\u4e24\u6b65", "zh"), {"number:2"})

    def test_percentage_is_a_metric_not_a_software_version(self):
        self.assertEqual(_versions("Memory grows by 8.9%"), set())
        self.assertEqual(_versions("\u5185\u5b58\u589e\u52a0 8.9%"), set())
        self.assertEqual(_versions("\u552e\u4ef7 24.98 \u4e07\u5143"), set())

    def test_schema_requires_one_verified_fact(self):
        with self.assertRaisesRegex(BilingualValidationError, "core_facts"):
            normalize_fact_schema({"article_id": "article-1", "category": "science", "core_facts": []}, "article-1")

    def test_one_field_repair_keeps_the_valid_item(self):
        now = datetime(2026, 9, 14, tzinfo=UTC)
        article = Article(
            "article-1", "science", "MIT launches AI", "MIT launches AI", "MIT News", "official_blog", now.isoformat(),
            "https://example.com/release", "en", "raw text", "MIT announced an AI tool.", "fingerprint-1", now.isoformat(), "source_verified",
        )
        item = {
            "id": "article-1", "category": "science", "importance_score": 80,
            "fact_schema": {
                "article_id": "article-1", "category": "science",
                "core_facts": [
                    {"id": "institution", "type": "institution", "value": "MIT", "rendered_in": ["title", "what_happened"], "english_forms": ["MIT"], "chinese_forms": ["\u9ebb\u7701\u7406\u5de5\u5b66\u9662"]},
                    {"id": "technology", "type": "technology", "value": "AI", "rendered_in": ["title", "what_happened"], "english_forms": ["AI"], "chinese_forms": ["AI"]},
                    {"id": "impact", "type": "impact", "value": "developers", "rendered_in": ["why_it_matters"], "english_forms": ["developers"], "chinese_forms": ["\u5f00\u53d1\u8005"]},
                ],
                "key_entities": ["MIT"], "dates": [], "numbers": [], "versions": [], "products": [], "companies": [], "technologies": ["AI"], "scope": "", "limitations": [], "importance_reasons": ["developers"],
            },
            "title_en": "MIT launches AI", "title_cn": "AI \u53d1\u5e03",
            "what_happened_en": "MIT announced a new AI tool.", "what_happened": "\u9ebb\u7701\u7406\u5de5\u5b66\u9662\u53d1\u5e03\u4e86\u4e00\u6b3e\u65b0 AI \u5de5\u5177\u3002",
            "why_it_matters_en": "It helps developers.", "why_it_matters": "\u5b83\u5e2e\u52a9\u5f00\u53d1\u8005\u3002",
        }
        usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            store.add(article)
            store.close()
            with patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                client.return_value.complete_json.side_effect = [
                    (json.dumps({"items": [item]}), usage, "deepseek-test"),
                    (json.dumps({"id": "article-1", "fields": {"title_cn": "\u9ebb\u7701\u7406\u5de5\u5b66\u9662\u63a8\u51fa AI"}}), usage, "deepseek-test"),
                ]
                result = run_enrichment(root, now=now)
            self.assertEqual(result.saved, 1)
            self.assertEqual(result.usage["calls"], 2)
            stored = ArticleStore(root / "data" / "ai_daily.sqlite3")
            try:
                self.assertEqual([row["task"] for row in stored.usage_rows()], [TASK_NAME, f"{TASK_NAME}_field_repair"])
            finally:
                stored.close()
            audit = json.loads((root / "data" / "bilingual-validation-audit.json").read_text(encoding="utf-8"))
            self.assertTrue(audit["entries"][0]["repaired"])

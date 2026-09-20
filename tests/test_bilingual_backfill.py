from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_daily_pipeline.bilingual_backfill import backfill_report


def legacy_item() -> dict[str, object]:
    return {
        "title_original": "Original title", "title_cn": "中文标题", "source": "Official",
        "published_at": "2026-09-20T00:00:00+00:00", "original_url": "https://example.com/source",
        "original_language": "en", "summary_en": "The company announced a release.",
        "summary_cn": "该公司发布了一项更新。", "relevance": "It affects developers.",
    }


class BilingualBackfillTests(unittest.TestCase):
    def test_backfill_rejects_changed_versions_or_acronyms(self):
        from ai_daily_pipeline.bilingual_backfill import BilingualBackfillError, _validated_pairs
        with self.assertRaisesRegex(BilingualBackfillError, "inconsistent bilingual literals"):
            _validated_pairs({"items": [{"id": "0", "title_en": "AI v1.0", "title_cn": "人工智能",
                                           "what_happened_en": "An update shipped.", "what_happened": "发布了更新。",
                                           "why_it_matters_en": "It matters.", "why_it_matters": "值得关注。"}]}, [legacy_item()])

    def test_dry_run_never_changes_the_historical_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline, site = root / "pipeline", root / "site"
            path = site / "data" / "daily" / "ai" / "2026-09-20.json"
            path.parent.mkdir(parents=True)
            original = {"schema_version": 1, "items": [legacy_item()]}
            path.write_text(json.dumps(original), encoding="utf-8")
            result = backfill_report(pipeline, site, "2026-09-20", dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_backfill_preserves_source_metadata_and_writes_v3_pairs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline, site = root / "pipeline", root / "site"
            (pipeline / "data").mkdir(parents=True)
            path = site / "data" / "daily" / "ai" / "2026-09-20.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"schema_version": 1, "category": "ai", "items": [legacy_item()]}), encoding="utf-8")
            index_path = site / "data" / "reports.json"
            index_path.write_text(json.dumps({"reports": [{"report_date": "2026-09-20", "category": "ai"}]}), encoding="utf-8")
            response = {"items": [{"id": "0", "title_en": "Release announced", "title_cn": "发布更新",
                                    "what_happened_en": "The company announced a release.", "what_happened": "该公司发布了一项更新。",
                                    "why_it_matters_en": "It affects developers.", "why_it_matters": "它会影响开发者。"}]}
            usage = {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40}
            with patch("ai_daily_pipeline.bilingual_backfill.DeepSeekClient") as client:
                client.return_value.complete_json.return_value = (json.dumps(response), usage, "deepseek-test")
                result = backfill_report(pipeline, site, "2026-09-20")
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["updated"], 1)
            self.assertEqual(report["schema_version"], 3)
            self.assertEqual(report["items"][0]["original_url"], "https://example.com/source")
            self.assertEqual(report["items"][0]["title_en"], "Release announced")
            self.assertEqual(report["items"][0]["why_it_matters"], "它会影响开发者。")
            self.assertEqual(json.loads(index_path.read_text(encoding="utf-8"))["reports"][0]["schema_version"], 3)

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_daily_pipeline.publish import PublishError, publish_latest_report


def valid_item(url: str = "https://example.com/report") -> dict[str, object]:
    return {
        "id": "verified-1", "title_cn": "测试标题", "title_original": "Original report",
        "source": "Official newsroom", "published_at": "2026-09-13T16:00:00+00:00",
        "original_url": url, "original_language": "en", "translation_language": "zh",
        "key_points_original": ["First sourced point", "Second sourced point"],
        "translation": ["第一条翻译", "第二条翻译"],
        "summary_cn": "中文摘要", "summary_en": "English summary", "relevance": "Why it matters",
        "useful_expressions": [],
    }


class PublishTests(unittest.TestCase):
    def test_publish_accepts_compact_tech_daily_schema(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline, site = root / "pipeline", root / "site"
            (pipeline / "data").mkdir(parents=True)
            item = {
                "title_cn": "芯片发布", "title_original": "Chip launch", "source": "Official",
                "published_at": "2026-09-13T16:00:00+00:00", "original_url": "https://example.com/chip",
                "original_language": "en", "category": "chips", "what_happened": "公司发布了新芯片。",
                "why_it_matters": "它改善了计算效率。", "importance_score": 82,
            }
            (pipeline / "data" / "latest-enrichment.json").write_text(json.dumps({
                "schema_version": 2, "generated_at": "2026-09-13T17:19:08+00:00", "items": [item]
            }, ensure_ascii=False), encoding="utf-8")
            output = publish_latest_report(pipeline, site)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["category"], "tech")
            self.assertEqual(report["items"][0]["what_happened"], "公司发布了新芯片。")

    def test_publish_creates_a_date_addressable_report_and_history_index(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = root / "pipeline"
            site = root / "site"
            (pipeline / "data").mkdir(parents=True)
            (pipeline / "data" / "latest-enrichment.json").write_text(json.dumps({
                "generated_at": "2026-09-13T17:19:08+00:00",
                "items": [valid_item()],
            }, ensure_ascii=False), encoding="utf-8")
            output = publish_latest_report(pipeline, site)
            self.assertEqual(output.relative_to(site).as_posix(), "data/daily/ai/2026-09-14.json")
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["article_count"], 1)
            self.assertEqual(report["estimated_reading_minutes"], 5)
            self.assertEqual(report["items"][0]["source"], "Official newsroom")
            self.assertEqual(report["items"][0]["original_url"], "https://example.com/report")
            self.assertEqual(report["items"][0]["original_language"], "en")
            history = json.loads((site / "data" / "reports.json").read_text(encoding="utf-8"))
            self.assertEqual(history["reports"][0]["report_date"], "2026-09-14")

            (pipeline / "data" / "latest-enrichment.json").write_text(json.dumps({
                "generated_at": "2026-09-14T17:19:08+00:00", "items": [valid_item("https://example.com/next")],
            }, ensure_ascii=False), encoding="utf-8")
            publish_latest_report(pipeline, site)
            dates = [entry["report_date"] for entry in json.loads((site / "data" / "reports.json").read_text(encoding="utf-8"))["reports"]]
            self.assertEqual(dates, ["2026-09-15", "2026-09-14"])
            self.assertTrue(output.is_file())

    def test_publish_rejects_blocked_content_even_when_url_is_allowed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = root / "pipeline"
            (pipeline / "data").mkdir(parents=True)
            (pipeline / "data" / "latest-enrichment.json").write_text(json.dumps({
                "generated_at": "2026-09-13T17:19:08+00:00",
                "items": [{**valid_item(), "title_cn": "blocked OpenAI reference"}],
            }), encoding="utf-8")
            with self.assertRaises(PublishError):
                publish_latest_report(pipeline, root / "site")

    def test_empty_or_replay_output_does_not_create_an_empty_daily_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = root / "pipeline"
            site = root / "site"
            (pipeline / "data").mkdir(parents=True)
            latest = pipeline / "data" / "latest-enrichment.json"
            for payload in (
                {"generated_at": "2026-09-15T00:00:00+00:00", "items": []},
                {"generated_at": "2026-09-10T00:00:00+00:00", "items": [valid_item()], "replay": True},
            ):
                latest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(PublishError):
                    publish_latest_report(pipeline, site)
            self.assertFalse((site / "data").exists())

    def test_source_metadata_is_required_before_a_report_can_be_published(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = root / "pipeline"
            site = root / "site"
            (pipeline / "data").mkdir(parents=True)
            for item in ({**valid_item(), "source": ""}, {**valid_item(), "original_url": "javascript:alert(1)"},
                         {**valid_item(), "original_language": ""}):
                (pipeline / "data" / "latest-enrichment.json").write_text(json.dumps({
                    "generated_at": "2026-09-15T00:00:00+00:00", "items": [item],
                }, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(PublishError):
                    publish_latest_report(pipeline, site)
            self.assertFalse((site / "data").exists())

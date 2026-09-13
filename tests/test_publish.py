import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_daily_pipeline.publish import publish_latest_report


class PublishTests(unittest.TestCase):
    def test_publish_creates_a_date_addressable_report_and_history_index(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = root / "pipeline"
            site = root / "site"
            (pipeline / "data").mkdir(parents=True)
            (pipeline / "data" / "latest-enrichment.json").write_text(json.dumps({
                "generated_at": "2026-09-13T17:19:08+00:00",
                "items": [{"title_cn": "测试标题", "summary_cn": "中文摘要", "summary_en": "English summary", "original_url": "https://example.com"}],
            }, ensure_ascii=False), encoding="utf-8")
            output = publish_latest_report(pipeline, site)
            self.assertEqual(output.relative_to(site).as_posix(), "data/daily/ai/2026-09-14.json")
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["article_count"], 1)
            self.assertEqual(report["estimated_reading_minutes"], 5)
            history = json.loads((site / "data" / "reports.json").read_text(encoding="utf-8"))
            self.assertEqual(history["reports"][0]["report_date"], "2026-09-14")

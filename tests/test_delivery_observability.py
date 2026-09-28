from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from zoneinfo import ZoneInfo

from ai_daily_pipeline.deepseek import DeepSeekError
from ai_daily_pipeline.diagnostics import mark_failure
from ai_daily_pipeline.delivery import run_scheduled_delivery


NOW = datetime(2026, 9, 28, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class ScheduledFailureObservabilityTests(unittest.TestCase):
    def _run(self, collection_error=None, enrichment_error=None):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("ai_daily_pipeline.delivery.run_collection", side_effect=collection_error) as collect, \
                 patch("ai_daily_pipeline.delivery.run_enrichment", side_effect=enrichment_error) as enrich, \
                 patch("ai_daily_pipeline.delivery.send_existing_report") as sender:
                if collection_error is None:
                    collect.return_value = SimpleNamespace(accepted=7)
                result = run_scheduled_delivery(root, root / "site", now=NOW)
            record = json.loads((root / "data/delivery-runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            sender.assert_not_called()
            if collection_error is not None:
                enrich.assert_not_called()
            return result, record

    def test_missing_collection_argument_is_logged_without_a_model_call(self):
        error = TypeError("run_collection() missing 1 required positional argument: 'dry_run'")
        result, record = self._run(collection_error=error)
        self.assertEqual(result.reason, "collection_failed")
        self.assertEqual(record["failure_stage"], "collection")
        self.assertIn("dry_run", record["error_summary"])
        self.assertEqual(record["candidate_count"], 0)
        self.assertFalse(record["feishu_send_attempted"])
        self.assertTrue(record["stack"])

    def test_enrichment_validation_and_persistence_have_distinct_statuses(self):
        cases = [
            (DeepSeekError("DeepSeek HTTP 503", usage={"total_tokens": 12}, model="deepseek-flash"), "enrichment", "enrichment_failed"),
            (mark_failure(ValueError("untrusted secret=never-log"), "validation", batch_index=2,
                          batch_article_ids=["article-1"], model="deepseek-flash", request_id=None,
                          token_usage={"total_tokens": 12}, local_repair_triggered=False), "validation", "validation_failed"),
            (sqlite3.OperationalError("database path secret=never-log"), "persistence", "persistence_failed"),
            (mark_failure(RuntimeError("secret=never-log"), "normalization"), "normalization", "normalization_failed"),
        ]
        for error, stage, status in cases:
            with self.subTest(stage=stage):
                result, record = self._run(enrichment_error=error)
                self.assertEqual(result.reason, status)
                self.assertEqual(record["failure_stage"], stage)
                self.assertEqual(record["candidate_count"], 7)
                self.assertNotIn("never-log", json.dumps(record))
                if stage == "enrichment":
                    self.assertEqual(record["http_status"], 503)
                if stage == "validation":
                    self.assertEqual(record["batch_index"], 2)
                    self.assertEqual(record["batch_article_ids"], ["article-1"])
                    self.assertEqual(record["token_usage"], {"total_tokens": 12})

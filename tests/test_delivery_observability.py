from __future__ import annotations

import json
import runpy
import sqlite3
import sys
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
from ai_daily_pipeline.pipeline import run_collection


NOW = datetime(2026, 9, 28, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class ScheduledFailureObservabilityTests(unittest.TestCase):
    def test_scheduled_entry_binds_required_collection_arguments_without_sending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("ai_daily_pipeline.delivery.run_collection", autospec=run_collection) as collect, \
                 patch("ai_daily_pipeline.delivery.run_enrichment") as enrich, \
                 patch("ai_daily_pipeline.delivery.publish_latest_report") as publish, \
                 patch("ai_daily_pipeline.delivery.send_existing_report") as sender:
                collect.return_value = SimpleNamespace(accepted=0, inserted=0)
                enrich.return_value = SimpleNamespace(candidates=0, saved=0, output_path=None, token_budget_status="normal")
                result = run_scheduled_delivery(root, root / "site", now=NOW)
            self.assertEqual(collect.call_args.args, (root,))
            self.assertFalse(collect.call_args.kwargs["dry_run"])
            self.assertIsNotNone(collect.call_args.kwargs["audit"])
            self.assertEqual(enrich.call_args.args, (root,))
            self.assertIsNotNone(enrich.call_args.kwargs["audit"])
            publish.assert_not_called()
            sender.assert_not_called()
            self.assertEqual(result.reason, "no_qualified_tech_news")

    def test_scheduled_cli_reaches_collection_without_network_or_send(self):
        script = Path(__file__).resolve().parents[1] / "run_daily_delivery.py"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(sys, "argv", [str(script), "--scheduled"]), \
                 patch("ai_daily_pipeline.delivery.run_scheduled_delivery",
                       side_effect=lambda _root, _site: run_scheduled_delivery(root, root / "site", now=NOW)) as scheduler, \
                 patch("ai_daily_pipeline.delivery.run_collection", autospec=run_collection) as collect, \
                 patch("ai_daily_pipeline.delivery.run_enrichment") as enrich, \
                 patch("ai_daily_pipeline.delivery.publish_latest_report") as publish, \
                 patch("ai_daily_pipeline.delivery.send_existing_report") as sender:
                collect.return_value = SimpleNamespace(accepted=0, inserted=0)
                enrich.return_value = SimpleNamespace(candidates=0, saved=0, output_path=None, token_budget_status="normal")
                runpy.run_path(str(script), run_name="__main__")
            scheduler.assert_called_once()
            self.assertEqual(collect.call_args.args, (root,))
            self.assertFalse(collect.call_args.kwargs["dry_run"])
            self.assertIsNotNone(collect.call_args.kwargs["audit"])
            publish.assert_not_called()
            sender.assert_not_called()

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

    def test_budget_exhaustion_before_minimum_never_publishes_or_sends(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("ai_daily_pipeline.delivery.run_collection", autospec=run_collection) as collect, \
                 patch("ai_daily_pipeline.delivery.run_enrichment") as enrich, \
                 patch("ai_daily_pipeline.delivery.publish_latest_report") as publish, \
                 patch("ai_daily_pipeline.delivery.send_existing_report") as sender:
                collect.return_value = SimpleNamespace(accepted=9, inserted=9)
                enrich.return_value = SimpleNamespace(candidates=9, saved=0, output_path=None,
                                                     token_budget_status="exhausted_before_minimum")
                result = run_scheduled_delivery(root, root / "site", now=NOW)
            self.assertEqual(result.status, "daily_failed")
            self.assertEqual(result.reason, "token_budget_exhausted")
            publish.assert_not_called()
            sender.assert_not_called()

from __future__ import annotations

from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import unittest

from ai_daily_pipeline.sources_readonly import available_dates, sources_for_date
from ai_daily_pipeline.sources_server import SourcesHandler

PROJECT = Path(__file__).resolve().parents[1]
DAY = "2026-09-28"


def setup_root(directory: str) -> Path:
    root = Path(directory)
    (root / "config").mkdir()
    shutil.copyfile(PROJECT / "config/sources.json", root / "config/sources.json")
    (root / "data/run-audits").mkdir(parents=True)
    return root


def save_audit(root: Path, run_id: str, end_time: str, articles: list[dict]) -> None:
    value = {"run_id": run_id, "date": DAY, "end_time": end_time,
             "sources": [{"source_id": "deepseek-news", "status": "ok", "items": 2}],
             "articles": articles, "final_status": "skipped"}
    (root / "data/run-audits" / f"{run_id}.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8")


def article(identifier: str, published: str, events: list[dict]) -> dict:
    return {"article_id": identifier, "trace_id": identifier,
            "source_id": "deepseek-news", "source": "DeepSeek Research & News",
            "title": f"Research result {identifier}", "published_at": published,
            "original_url": f"https://www.deepseek.com/en/news/{identifier}",
            "current_stage": events[-1]["stage"] if events else None,
            "final_status": "dropped", "final_reason": "reason_x", "events": events}


class SourcesDataTests(unittest.TestCase):
    def test_date_run_selection_and_descending_articles(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            save_audit(root, "early", "2026-09-28T01:00:00+00:00", [article("old", "2026-09-28T01:00:00+00:00", [])])
            save_audit(root, "late", "2026-09-28T02:00:00+00:00", [
                article("older", "2026-09-28T00:00:00+00:00", []),
                article("newer", "2026-09-28T02:00:00+00:00", [])])
            value = sources_for_date(root, DAY)
            self.assertEqual(value["run_id"], "late")
            self.assertEqual([x["article_id"] for x in value["articles"]], ["newer", "older"])
            self.assertEqual(sources_for_date(root, DAY, "early")["visible_article_count"], 1)
            self.assertEqual(available_dates(root), [DAY])
            self.assertEqual(value["source_count"], 29)

    def test_stage_trace_and_drop_reason(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            save_audit(root, "only", "2026-09-28T02:00:00+00:00", [article("one", "2026-09-28T01:00:00+00:00", [
                {"stage": "collection", "status": "kept", "reason": "parsed_source_item"},
                {"stage": "normalization", "status": "kept", "reason": "extracted_content"},
                {"stage": "initial_candidate_pool", "status": "kept", "reason": "eligible"},
                {"stage": "validation", "status": "dropped", "reason": "hard_facts"}])])
            row = sources_for_date(root, DAY)["articles"][0]
            self.assertEqual(row["stages"]["collected"], "kept")
            self.assertEqual(row["stages"]["normalized"], "kept")
            self.assertEqual(row["stages"]["candidate"], "kept")
            self.assertEqual(row["stages"]["validation"], "dropped")
            self.assertEqual(row["stages"]["publishable"], "not_reached")
            self.assertEqual(row["reason"], "hard_facts")
            self.assertEqual(row["trace_id"], "one")
            self.assertEqual(row["article_id"], "one")
            self.assertEqual([x["stage"] for x in row["events"]],
                             ["collection", "normalization", "initial_candidate_pool", "validation"])

    def test_known_early_drop_is_not_reported_as_unavailable(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            save_audit(root, "only", "2026-09-28T02:00:00+00:00", [article("one", "2026-09-28T01:00:00+00:00", [
                {"stage": "collection", "status": "kept", "reason": "parsed_source_item"},
                {"stage": "feed_freshness", "status": "dropped", "reason": "outside_168h_feed_window"}])])
            row = sources_for_date(root, DAY)["articles"][0]
            self.assertEqual(row["stages"]["normalized"], "not_reached")
            self.assertEqual(row["stages"]["candidate"], "not_reached")
            self.assertEqual(row["stages"]["publishable"], "not_reached")
            self.assertEqual(row["reason"], "outside_168h_feed_window")

    def test_older_failed_repair_proves_validation_failed_without_inventing_reason(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            save_audit(root, "only", "2026-09-28T02:00:00+00:00", [article("one", "2026-09-28T01:00:00+00:00", [
                {"stage": "enrichment", "status": "kept", "reason": "batch_requested"},
                {"stage": "repair", "status": "dropped", "reason": "repair_failed"}])])
            row = sources_for_date(root, DAY)["articles"][0]
            self.assertEqual(row["stages"]["validation"], "dropped")
            self.assertEqual(row["reason"], "repair_failed")
            self.assertFalse(any(event["stage"] == "validation" for event in row["events"]))

    def test_later_candidate_drop_does_not_invent_current_run_normalization(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            save_audit(root, "only", "2026-09-28T02:00:00+00:00", [article("one", "2026-09-28T01:00:00+00:00", [
                {"stage": "initial_quality_filter", "status": "dropped", "reason": "editorial_exclusion"}])])
            row = sources_for_date(root, DAY)["articles"][0]
            self.assertEqual(row["stages"]["collected"], "unavailable")
            self.assertEqual(row["stages"]["normalized"], "unavailable")
            self.assertEqual(row["stages"]["candidate"], "dropped")
            self.assertEqual(row["stages"]["enrichment"], "not_reached")

    def test_no_audit_does_not_invent_status_or_articles(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            value = sources_for_date(root, DAY)
            self.assertEqual(value["source_count"], 29)
            self.assertEqual(value["visible_article_count"], 0)
            self.assertEqual(value["sources"][0]["status"], "unavailable")
            self.assertEqual(value["sources"][0]["article_count"], "unavailable")

    def test_latest_run_snapshot_only_marks_downstream_unavailable(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            latest = {"source_health": [{"source_id": "deepseek-news", "status": "ok", "items": 1,
                                         "checked_at": "2026-09-28T01:00:00+00:00"}],
                      "articles": [{"id": "one", "source": "DeepSeek Research & News", "title": "A real title",
                                    "published_at": "2026-09-28T01:00:00+00:00",
                                    "original_url": "https://www.deepseek.com/en/news/one"}]}
            (root / "data/latest-run.json").write_text(json.dumps(latest), encoding="utf-8")
            row = sources_for_date(root, DAY)["articles"][0]
            self.assertEqual(row["stages"]["collected"], "kept")
            self.assertEqual(row["stages"]["validation"], "unavailable")
            self.assertEqual(row["original_url"], "https://www.deepseek.com/en/news/one")
            self.assertEqual(row["trace_id"], "unavailable")

    def test_reading_sources_never_collects_or_calls_model(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            with patch("ai_daily_pipeline.sources.collect_source", side_effect=AssertionError("network collection")), \
                 patch("ai_daily_pipeline.deepseek.DeepSeekClient", side_effect=AssertionError("model call")):
                self.assertEqual(sources_for_date(root, DAY)["source_count"], 29)

    def test_invalid_date_and_run_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            for invalid in ("2026-09-31", "../config", "2026/09/28"):
                with self.assertRaises(ValueError):
                    sources_for_date(root, invalid)
            with self.assertRaises(KeyError):
                sources_for_date(root, DAY, "unknown-run")


class SourcesRouteTests(unittest.TestCase):
    def test_loopback_routes_and_host_restriction(self):
        with TemporaryDirectory() as directory:
            root = setup_root(directory)
            save_audit(root, "one", "2026-09-28T01:00:00+00:00", [article("one", "2026-09-28T01:00:00+00:00", [])])

            class Handler(SourcesHandler):
                pass

            Handler.root = root
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    with urlopen(base + "/sources") as response:
                        self.assertIn(b"Sources", response.read())
                        self.assertIn("'self'", response.headers["Content-Security-Policy"])
                    with urlopen(base + "/api/sources?date=" + DAY) as response:
                        value = json.load(response)
                        self.assertEqual(value["source_count"], 29)
                        self.assertEqual(value["visible_article_count"], 1)
                        self.assertEqual(value["articles"][0]["trace_id"], "one")
                    with urlopen(base + "/assets/sources.js") as response:
                        self.assertIn(b'trace_id', response.read())
                    with self.assertRaises(HTTPError) as invalid:
                        urlopen(base + "/api/sources?date=../../etc")
                    self.assertEqual(invalid.exception.code, 400)
                    with self.assertRaises(HTTPError) as forbidden:
                        urlopen(Request(base + "/api/sources/dates", headers={"Host": "outside.example"}))
                    self.assertEqual(forbidden.exception.code, 403)
                    with self.assertRaises(HTTPError) as unknown:
                        urlopen(base + "/data/latest-run.json")
                    self.assertEqual(unknown.exception.code, 404)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

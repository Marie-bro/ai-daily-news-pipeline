import json
import time
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from ai_daily_pipeline.delivery import (PublishVerificationError, daily_url, deploy_site_data,
                                       run_scheduled_delivery, send_existing_report, verify_public_report)
from test_delivery import DATE, NOW, ITEM, Response, FakeClock, scripted_opener


class ReadinessStateTests(unittest.TestCase):
    def verify(self, opener, *, clock=None, context=None):
        clock = clock or FakeClock()
        summary, attempts = {}, []
        verify_public_report({"report_date": DATE, "items": [ITEM]}, daily_url(DATE), opener=opener,
                             clock=clock, sleeper=clock.sleep,
                             wall_clock=lambda: NOW + timedelta(seconds=clock.seconds),
                             readiness_log=summary, attempt_log=attempts, deployment_context=context)
        return clock, summary, attempts

    def test_immediate_readiness_and_push_relative_observations(self):
        context = {"deploy_commit_sha": "a" * 40, "deploy_push_finished_time": (NOW - timedelta(seconds=2)).isoformat()}
        clock, summary, attempts = self.verify(scripted_opener([200], [200]), context=context)
        self.assertEqual(summary["readiness_result"], "ready")
        self.assertEqual(summary["readiness_total_ms"], 0)
        self.assertEqual(summary["h5_first_200_after_ms"], 2000)
        self.assertEqual(summary["json_first_200_after_ms"], 2000)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(clock.sleeps, [])
        for entry in attempts:
            for field in ["deploy_commit_sha", "deploy_push_finished_time", "readiness_start_time", "deadline", "url", "attempt",
                          "timestamp", "status", "exception_type", "elapsed_since_push_ms", "first_200_at"]:
                self.assertIn(field, entry)

    def test_json_404_is_pending_and_ready_h5_is_not_repeated(self):
        context = {"deploy_commit_sha": "b" * 40, "deploy_push_finished_time": NOW.isoformat()}
        clock, summary, attempts = self.verify(scripted_opener([200], [404, 404, 200]), context=context)
        self.assertEqual([x["kind"] for x in attempts], ["h5", "json", "json", "json"])
        self.assertEqual([x["status"] for x in attempts], ["ready", "not_ready_yet", "not_ready_yet", "ready"])
        self.assertEqual(summary["json_first_200_after_ms"], 15000)
        self.assertEqual(summary["readiness_total_ms"], 15000)
        self.assertEqual(clock.seconds, 15)

    def test_deadline_not_a_fixed_attempt_cap(self):
        with patch("ai_daily_pipeline.delivery.PUBLIC_VERIFY_DELAYS_SECONDS", (1,)):
            clock, summary, attempts = self.verify(scripted_opener([200], [404] * 12 + [200]))
        self.assertEqual(max(x["attempt"] for x in attempts), 13)
        self.assertEqual(clock.seconds, 12)
        self.assertEqual(summary["readiness_result"], "ready")

    def test_either_endpoint_stays_404_until_deadline(self):
        for failed_kind in ["h5", "json"]:
            with self.subTest(kind=failed_kind):
                clock, summary, attempts = FakeClock(), {}, []
                opener = scripted_opener([404] * 9 if failed_kind == "h5" else [200],
                                         [404] * 9 if failed_kind == "json" else [200])
                with self.assertRaises(PublishVerificationError):
                    verify_public_report({"report_date": DATE}, daily_url(DATE), opener=opener,
                                         clock=clock, sleeper=clock.sleep, readiness_log=summary, attempt_log=attempts)
                self.assertEqual(clock.seconds, 210)
                self.assertEqual(summary["readiness_result"], "publish_verification_failed")
                self.assertEqual(summary["readiness_total_ms"], 210000)
                self.assertIsNone(summary[f"{failed_kind}_first_200_at"])

    def test_timeout_429_and_5xx_recover_without_bypassing_readiness(self):
        for failure in [URLError(TimeoutError("timeout")), HTTPError(daily_url(DATE), 429, "busy", {}, None), 503]:
            with self.subTest(failure=type(failure).__name__):
                clock, summary, attempts = self.verify(scripted_opener([200], [failure, 200]))
                self.assertEqual(summary["readiness_result"], "ready")
                self.assertEqual(clock.seconds, 5)
                self.assertEqual(attempts[1]["status"], "not_ready_yet")

    def test_exact_deadline_response_is_not_accepted(self):
        clock = FakeClock()
        def opener(request, timeout):
            if request.full_url.endswith(".json"):
                clock.seconds = 210
                return Response(json.dumps({"report_date": DATE, "items": [ITEM]}).encode())
            return Response()
        summary = {}
        with self.assertRaises(PublishVerificationError):
            verify_public_report({"report_date": DATE}, daily_url(DATE), opener=opener,
                                 clock=clock, sleeper=clock.sleep, readiness_log=summary)
        self.assertEqual(summary["readiness_result"], "publish_verification_failed")
        self.assertEqual(summary["readiness_total_ms"], 210000)

    def test_empty_or_wrong_date_json_200_is_not_ready(self):
        replies = iter([{"report_date": DATE, "items": []}, {"report_date": "2026-09-19", "items": [ITEM]},
                        {"report_date": DATE, "items": [ITEM]}])
        def opener(request, timeout):
            return Response(json.dumps(next(replies)).encode()) if request.full_url.endswith(".json") else Response()
        _, summary, attempts = self.verify(opener)
        self.assertEqual([x["failure_type"] for x in attempts[1:]], ["invalid_report", "invalid_report", None])
        self.assertEqual(summary["readiness_total_ms"], 15000)

    def test_missing_push_time_is_not_fabricated(self):
        _, summary, attempts = self.verify(scripted_opener([200], [200]))
        self.assertIsNone(summary["deploy_push_finished_time"])
        self.assertIsNone(summary["json_first_200_after_ms"])
        self.assertIsNone(attempts[0]["elapsed_since_push_ms"])

    def test_404_cache_headers_are_preserved_without_claiming_cache_cause(self):
        error = HTTPError(daily_url(DATE), 404, "pending", {"Age": "12", "EO-Cache-Status": "Cache Hit"}, None)
        _, _, attempts = self.verify(scripted_opener([200], [error, 200]))
        self.assertEqual(attempts[1]["response_cache_headers"], {"Age": "12", "EO-Cache-Status": "Cache Hit"})
        self.assertEqual(attempts[1]["failure_type"], "http_non_200")

    def test_blocking_probe_cannot_hold_caller_past_deadline_or_send(self):
        def opener(*_args, **_kwargs):
            time.sleep(.2)
            return Response()
        summary = {}
        started = time.monotonic()
        with patch("ai_daily_pipeline.delivery.PUBLIC_VERIFY_MAX_WAIT_SECONDS", .03):
            with self.assertRaises(PublishVerificationError):
                verify_public_report({"report_date": DATE}, daily_url(DATE), opener=opener, readiness_log=summary)
        self.assertLess(time.monotonic() - started, .18)
        self.assertEqual(summary["readiness_result"], "publish_verification_failed")

    def test_scheduled_path_persists_deploy_and_readiness_audit_and_sends_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "site"
            report_path = site / "data/daily/ai" / (DATE + ".json")
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps({"schema_version": 3, "report_date": DATE, "article_count": 1,
                                              "estimated_reading_minutes": 1, "items": [ITEM]}), encoding="utf-8")
            receipt = {"deploy_commit_sha": "c" * 40, "deploy_push_finished_time": NOW.isoformat(),
                       "deploy_push_status": "pushed", "deployment_id": None, "deployment_status": "unknown"}
            clock, sends = FakeClock(), []
            real_send = send_existing_report
            def send(*args, **kwargs):
                kwargs.update(sender=lambda *_: (sends.append("sent") or "message", 0),
                              target=("open_id", "test-owner"), opener=scripted_opener([200], [404, 200]),
                              verification_clock=clock, verification_sleeper=clock.sleep,
                              verification_wall_clock=lambda: NOW + timedelta(seconds=clock.seconds))
                return real_send(*args, **kwargs)
            with patch("ai_daily_pipeline.delivery.run_collection", return_value=SimpleNamespace(accepted=1, inserted=1)), \
                 patch("ai_daily_pipeline.delivery.run_enrichment", return_value=SimpleNamespace(saved=1, output_path=root / "data/latest-enrichment.json")), \
                 patch("ai_daily_pipeline.delivery.publish_latest_report", return_value=report_path), \
                 patch("ai_daily_pipeline.delivery.deploy_site_data", return_value=receipt), \
                 patch("ai_daily_pipeline.delivery.send_existing_report", side_effect=send):
                result = run_scheduled_delivery(root, site, now=NOW)
            self.assertEqual(result.status, "sent")
            self.assertEqual(sends, ["sent"])
            audit = json.loads(next((root / "data/run-audits").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(audit["metrics"]["deploy_commit_sha"], "c" * 40)
            self.assertEqual(audit["metrics"]["readiness_result"], "ready")
            self.assertEqual(audit["metrics"]["json_first_200_after_ms"], 5000)
            self.assertEqual(audit["metrics"]["feishu_send_result"], "sent")

    def test_deadline_failure_is_audited_without_send_or_delivery_record(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "site"
            report_path = site / "data/daily/ai" / (DATE + ".json")
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps({"schema_version": 3, "report_date": DATE, "article_count": 1,
                                              "estimated_reading_minutes": 1, "items": [ITEM]}), encoding="utf-8")
            audit, clock, sends = SimpleNamespace(metrics={}), FakeClock(), []
            result = send_existing_report(root, site, DATE, now=NOW, audit=audit,
                                          sender=lambda *_: sends.append("unexpected"), target=("open_id", "test-owner"),
                                          opener=scripted_opener([200], [404] * 9),
                                          verification_clock=clock, verification_sleeper=clock.sleep)
            self.assertEqual(result.reason, "publish_verification_failed")
            self.assertEqual(sends, [])
            self.assertFalse((root / "data/news.sqlite3").exists())
            self.assertEqual(audit.metrics["readiness_result"], "publish_verification_failed")
            self.assertEqual(audit.metrics["readiness_total_ms"], 210000)
            log = json.loads((root / "data/delivery-runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertFalse(log["feishu_send_attempted"])
            self.assertEqual(log["skipped_reason"], "publish_verification_failed")

    def test_push_receipt_records_only_push_success_not_edgeone_completion(self):
        responses = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
                     SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout="d" * 40)]
        with patch("ai_daily_pipeline.delivery.subprocess.run", side_effect=responses) as git, \
             patch("ai_daily_pipeline.delivery._now", return_value=NOW):
            receipt = deploy_site_data(Path("site"), Path("site/data/daily/ai") / (DATE + ".json"))
        self.assertEqual(receipt["deploy_commit_sha"], "d" * 40)
        self.assertEqual(receipt["deploy_push_finished_time"], NOW.isoformat())
        self.assertEqual(receipt["deployment_status"], "unknown")
        self.assertIsNone(receipt["deployment_id"])
        self.assertEqual(git.call_args_list[3].args[0], ["git", "push", "origin", "main"])

    def test_no_changes_does_not_invent_a_push_timestamp(self):
        with patch("ai_daily_pipeline.delivery.subprocess.run", side_effect=[SimpleNamespace(returncode=0),
                   SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout="e" * 40)]) as git:
            receipt = deploy_site_data(Path("site"), Path("site/data/daily/ai") / (DATE + ".json"))
        self.assertIsNone(receipt["deploy_push_finished_time"])
        self.assertEqual(receipt["deploy_push_status"], "not_attempted_no_changes")
        self.assertFalse(any("push" in x.args[0] for x in git.call_args_list))


if __name__ == "__main__":
    unittest.main()

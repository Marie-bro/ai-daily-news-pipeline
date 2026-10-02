from __future__ import annotations

import io
import json
import socket
import ssl
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import URLError
from zoneinfo import ZoneInfo

from ai_daily_pipeline.delivery import PublishVerificationError, card_for_report, daily_url, send_existing_report, verify_public_report


DATE = "2026-09-20"
NOW = datetime(2026, 9, 21, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class FakeClock:
    def __init__(self):
        self.seconds = 0.0
        self.sleeps = []

    def __call__(self): return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds
ITEM = {
    "title_en": "Example release v1.2", "title_cn": "示例发布 v1.2", "title_original": "Example release v1.2",
    "what_happened_en": "Example Corp released v1.2.", "what_happened": "示例公司发布了 v1.2。",
    "why_it_matters_en": "It is a verified update.", "why_it_matters": "这是已核实的更新。",
    "source": "Example Corp", "published_at": "2026-09-20T00:00:00+00:00", "original_url": "https://example.com/news",
}


class Response:
    status = 200

    def __init__(self, body: bytes = b"", status: int = 200):
        self.body = io.BytesIO(body)
        self.status = status

    def read(self):
        return self.body.read()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def public_opener(request, timeout=20):
    if request.full_url.endswith(".json"):
        return Response(json.dumps({"report_date": DATE, "items": [ITEM]}).encode())
    return Response()


def scripted_opener(h5: list[int | Exception], data: list[int | Exception]):
    outcomes = {"h5": iter(h5), "json": iter(data)}

    def open_request(request, timeout=20):
        kind = "json" if request.full_url.endswith(".json") else "h5"
        outcome = next(outcomes[kind])
        if isinstance(outcome, Exception):
            raise outcome
        body = json.dumps({"report_date": DATE, "items": [ITEM]}).encode() if kind == "json" else b""
        return Response(body, status=outcome)

    return open_request


class DeliveryTests(unittest.TestCase):
    def write_report(self, root: Path) -> Path:
        site = root / "site"
        path = site / "data" / "daily" / "ai" / f"{DATE}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 3, "report_date": DATE, "article_count": 1,
                                    "estimated_reading_minutes": 5, "items": [ITEM]}, ensure_ascii=False), encoding="utf-8")
        return site

    def test_card_uses_existing_bilingual_titles_and_formal_url(self):
        report = {"report_date": DATE, "article_count": 1, "estimated_reading_minutes": 5, "items": [ITEM]}
        card = card_for_report(report, daily_url(DATE))
        self.assertEqual(card["header"]["title"]["content"], "MarieSpace Radar\nMarieSpace \u6bcf\u65e5\u96f7\u8fbe")
        content = json.dumps(card, ensure_ascii=False)
        self.assertLess(content.index(ITEM["title_en"]), content.index(ITEM["title_cn"]))
        self.assertLess(content.index(ITEM["what_happened_en"]), content.index(ITEM["what_happened"]))
        self.assertLess(content.index(ITEM["why_it_matters_en"]), content.index(ITEM["why_it_matters"]))
        self.assertIn("What happened?", content)
        self.assertIn("\u53d1\u751f\u4e86\u4ec0\u4e48\uff1f", content)
        self.assertIn("Why it matters?", content)
        self.assertIn("\u4e3a\u4ec0\u4e48\u503c\u5f97\u5173\u6ce8\uff1f", content)
        self.assertIn("View MarieSpace Radar", content)
        self.assertIn("\u67e5\u770b MarieSpace \u6bcf\u65e5\u96f7\u8fbe", content)
        self.assertIn("Save for Later", content)
        self.assertIn("\u6536\u85cf", content)
        self.assertIn(daily_url(DATE), content)
        self.assertIn("&favorite=", content)
        self.assertNotIn("what_happened_en", content)
        self.assertNotIn("original_url", content)

    def test_degraded_cards_use_simple_user_facing_titles(self):
        report = {"report_date": DATE, "article_count": 1, "estimated_reading_minutes": 5, "items": [ITEM]}
        report["daily_mode"] = "graceful_degraded"
        self.assertEqual(card_for_report(report, daily_url(DATE))["header"]["title"]["content"],
                         "MarieSpace Radar · Compact Edition\nMarieSpace 每日雷达 · 精简版")
        report["daily_mode"] = "minimal_daily"
        self.assertEqual(card_for_report(report, daily_url(DATE))["header"]["title"]["content"],
                         "MarieSpace Today's Watch\nMarieSpace 今日观察")

    def test_delayed_json_readiness_sends_only_after_both_urls_work(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = self.write_report(root)
            sent = []
            clock = FakeClock()
            def sender(_target_type, _target_id, _card, request_id):
                sent.append(request_id)
                return "om_delayed_ready", 0
            result = send_existing_report(root, site, DATE, now=NOW,
                                          sender=sender, target=("open_id", "ou_test"),
                                          opener=scripted_opener([200, 200], [404, 200]),
                                          verification_sleeper=clock.sleep, verification_clock=clock)
            self.assertEqual(result.status, "sent")
            self.assertEqual(len(sent), 1)
            log = json.loads((root / "data" / "delivery-runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual([row["http_status"] for row in log["verification_attempts"]], [200, 404, 200])
            self.assertTrue(log["url_reachable"])
            self.assertTrue(log["feishu_send_attempted"])

    def test_real_delivery_is_idempotent_and_force_is_explicit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = self.write_report(root)
            sent: list[str] = []

            def sender(_target_type, _target_id, _card, request_id):
                sent.append(request_id)
                return "om_phase6_test", 1

            common = {"now": NOW, "sender": sender, "target": ("open_id", "ou_phase6_test"), "opener": public_opener}
            first = send_existing_report(root, site, DATE, **common)
            second = send_existing_report(root, site, DATE, **common)
            forced = send_existing_report(root, site, DATE, force=True, **common)
            self.assertEqual(first.status, "sent")
            self.assertEqual(first.message_id, "om_phase6_test")
            self.assertEqual(second.status, "skipped")
            self.assertEqual(forced.status, "sent")
            self.assertEqual(len(sent), 2)

    def test_dry_runs_never_call_public_url_or_sender(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = self.write_report(root)
            result = send_existing_report(root, site, DATE, dry_run=True, now=NOW,
                                          sender=lambda *_args: self.fail("must not send"), target=("open_id", "ou_test"))
            card_result = send_existing_report(root, site, DATE, send_dry_run=True, now=NOW,
                                               sender=lambda *_args: self.fail("must not send"), target=("open_id", "ou_test"))
            self.assertEqual(result.status, "dry_run")
            self.assertEqual(card_result.status, "dry_run")

    def test_url_verification_never_sends(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = self.write_report(root)
            result = send_existing_report(root, site, DATE, verify_only=True, now=NOW,
                                          sender=lambda *_args: self.fail("must not send"), target=("open_id", "ou_test"), opener=public_opener)
            self.assertEqual(result.status, "verified")
            self.assertEqual(result.reason, "verified_without_send")

    def test_unreachable_formal_page_blocks_sending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            site = self.write_report(root)
            clock = FakeClock()
            result = send_existing_report(root, site, DATE, now=NOW, target=("open_id", "ou_test"),
                                          sender=lambda *_args: self.fail("must not send"), opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
                                          verification_sleeper=clock.sleep, verification_clock=clock)
            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "publish_verification_failed")
            record = json.loads((root / "data" / "delivery-runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["skipped_reason"], "publish_verification_failed")
            self.assertFalse(record["feishu_send_attempted"])
            self.assertEqual(len(record["verification_attempts"]), 18)


class PublicVerificationRetryTests(unittest.TestCase):
    def setUp(self):
        self.report = {"report_date": DATE, "items": [ITEM]}
        self.url = daily_url(DATE)

    def verify(self, h5, data):
        attempts = []
        clock = FakeClock()
        verify_public_report(self.report, self.url, opener=scripted_opener(h5, data),
                             sleeper=clock.sleep, clock=clock, attempt_log=attempts)
        return attempts, clock.sleeps

    def test_first_failure_second_success(self):
        attempts, sleeps = self.verify([503, 200], [200, 200])
        self.assertEqual(sleeps, [5])
        self.assertEqual([(row["attempt"], row["kind"], row["http_status"]) for row in attempts],
                         [(1, "h5", 503), (1, "json", 200), (2, "h5", 200)])
        self.assertEqual(attempts[0]["failure_type"], "http_non_200")
        self.assertIn("timestamp", attempts[0])
        self.assertTrue(all(row["url"].startswith("https://news.mariespace.cn/") and row["elapsed_ms"] >= 0 for row in attempts))

    def test_multiple_failures_then_success(self):
        attempts, sleeps = self.verify([503, 503, 200], [503, 200, 200])
        self.assertEqual(sleeps, [5, 10])
        self.assertEqual(len(attempts), 5)
        self.assertTrue(all(row["failure_type"] is None for row in attempts[-2:]))

    def test_final_failure_is_bounded(self):
        attempts = []
        clock = FakeClock()
        with self.assertRaises(PublishVerificationError):
            verify_public_report(self.report, self.url, opener=scripted_opener([503] * 9, [503] * 9),
                                 sleeper=clock.sleep, clock=clock, attempt_log=attempts)
        self.assertEqual(clock.sleeps, [5, 10, 15, 30, 30, 30, 30, 30, 30])
        self.assertEqual(clock.seconds, 210)
        self.assertEqual(len(attempts), 18)

    def test_json_becomes_ready_after_original_five_attempt_window(self):
        attempts, sleeps = self.verify([200] * 6, [404] * 5 + [200])
        self.assertEqual(len(attempts), 7)
        self.assertEqual(len(sleeps), 5)
        self.assertEqual(attempts[-1]["kind"], "json")
        self.assertEqual(attempts[-1]["http_status"], 200)

    def test_hard_deadline_stops_even_when_requests_are_slow(self):
        now = [0.0]
        attempts = []
        def slow_opener(_request, timeout=20):
            self.assertLessEqual(timeout, 20)
            now[0] += 110
            return Response(status=503)
        with self.assertRaises(PublishVerificationError):
            verify_public_report(self.report, self.url, opener=slow_opener,
                                 sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                                 clock=lambda: now[0], attempt_log=attempts)
        self.assertEqual(len(attempts), 2)

    def test_h5_success_json_failure_still_blocks(self):
        attempts = []
        clock = FakeClock()
        with self.assertRaises(PublishVerificationError):
            verify_public_report(self.report, self.url, opener=scripted_opener([200] * 9, [503] * 9),
                                 sleeper=clock.sleep, clock=clock, attempt_log=attempts)
        self.assertEqual([row["kind"] for row in attempts if row["failure_type"]], ["json"] * 9)

    def test_json_success_h5_failure_still_blocks(self):
        attempts = []
        clock = FakeClock()
        with self.assertRaises(PublishVerificationError):
            verify_public_report(self.report, self.url, opener=scripted_opener([503] * 9, [200] * 9),
                                 sleeper=clock.sleep, clock=clock, attempt_log=attempts)
        self.assertEqual([row["kind"] for row in attempts if row["failure_type"]], ["h5"] * 9)

    def test_timeout_dns_and_tls_are_distinct(self):
        cases = [(URLError(socket.timeout("timed out")), "timeout"),
                 (URLError(socket.gaierror("DNS lookup failed")), "dns"),
                 (URLError(ssl.SSLError("certificate verify failed")), "tls")]
        for error, kind in cases:
            with self.subTest(kind=kind):
                attempts, _sleeps = self.verify([error, 200], [200, 200])
                self.assertEqual(attempts[0]["failure_type"], kind)
                self.assertEqual(attempts[0]["exception_type"], "URLError")
                self.assertIn(kind, {"timeout", "dns", "tls"})

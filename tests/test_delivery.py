from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from ai_daily_pipeline.delivery import card_for_report, daily_url, send_existing_report


DATE = "2026-09-20"
NOW = datetime(2026, 9, 21, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
ITEM = {
    "title_en": "Example release v1.2", "title_cn": "示例发布 v1.2", "title_original": "Example release v1.2",
    "what_happened_en": "Example Corp released v1.2.", "what_happened": "示例公司发布了 v1.2。",
    "why_it_matters_en": "It is a verified update.", "why_it_matters": "这是已核实的更新。",
    "source": "Example Corp", "published_at": "2026-09-20T00:00:00+00:00", "original_url": "https://example.com/news",
}


class Response:
    status = 200

    def __init__(self, body: bytes = b""):
        self.body = io.BytesIO(body)

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
        self.assertEqual(card["header"]["title"]["content"], "MarieSpace Tech Daily\nMarieSpace 科技日报")
        content = json.dumps(card, ensure_ascii=False)
        self.assertLess(content.index(ITEM["title_en"]), content.index(ITEM["title_cn"]))
        self.assertIn(daily_url(DATE), content)
        self.assertNotIn("what_happened_en", content)
        self.assertNotIn("original_url", content)

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
            result = send_existing_report(root, site, DATE, now=NOW, target=("open_id", "ou_test"),
                                          sender=lambda *_args: self.fail("must not send"), opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
            self.assertEqual(result.status, "skipped")
            self.assertIn("formal daily URL", result.reason)

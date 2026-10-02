from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ai_daily_pipeline.bilingual import BilingualValidationError
from ai_daily_pipeline.enrich import (
    ItemValidation, _budget_status, _daily_mode, _estimate_request_tokens, run_enrichment,
)
from ai_daily_pipeline.models import Article, Enrichment
from ai_daily_pipeline.publish import publish_latest_report
from ai_daily_pipeline.store import ArticleStore


NOW = datetime(2026, 9, 14, tzinfo=UTC)


def article(index: int) -> Article:
    stamp = NOW.isoformat()
    return Article(
        f"article-{index}", "ai", f"Distinct technology release {index}", f"Distinct technology release {index}",
        f"Official source {index}", "official_blog", stamp, f"https://example.com/release-{index}",
        "en", "raw " * 60, "Verified details of this technology release. " * 20,
        f"fingerprint-{index}", stamp, "source_verified", "US", 1, "primary", "technology",
    )


def enrichment(value: Article, generated_at: str) -> Enrichment:
    return Enrichment(
        value.id, "phase5_5_bilingual_tech_daily", generated_at, "deepseek-test",
        f"技术发布 {value.id}", f"Technology release {value.id}", value.original_title,
        value.source, value.published_at, value.original_url, value.category, "en",
        "公司发布了一项技术。", "The company released a technology.",
        "这项技术值得关注。", "This technology matters.", 80,
    )


class TokenBudgetTests(unittest.TestCase):
    def test_exact_limit_is_allowed_but_one_token_over_stops(self):
        self.assertEqual(_budget_status(10, 35_000, 40_000, 5_000, 10), "normal")
        self.assertEqual(_budget_status(10, 35_001, 40_000, 5_000, 10), "graceful_stop")
        self.assertEqual(_budget_status(9, 35_001, 40_000, 5_000, 10), "graceful_stop")
        self.assertEqual(_budget_status(0, 35_001, 40_000, 5_000, 10), "exhausted_before_minimum")

    def test_estimator_reserves_output_and_safety_and_respects_observed_input(self):
        baseline = _estimate_request_tokens("system", "candidate" * 100, 7_200)
        observed = _estimate_request_tokens("system", "candidate" * 100, 7_200, .85)
        self.assertGreaterEqual(baseline.safety_tokens, 256)
        self.assertGreater(observed.input_tokens, baseline.input_tokens)
        self.assertEqual(observed.required_tokens, observed.input_tokens + 7_200 + observed.safety_tokens)
        self.assertEqual(_budget_status(12, 35_730, 40_000, observed.required_tokens, 10), "graceful_stop")

    def _run_case(self, initial_count: int, per_batch_tokens: int, *, repair: bool = False,
                  configured_budget: int = 40_000, override: int | None = None,
                  fallback_count: int = 6):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            initial = [article(index) for index in range(initial_count)]
            fallback = [article(index) for index in range(initial_count, initial_count + fallback_count)]
            store = ArticleStore(root / "data" / "ai_daily.sqlite3")
            for value in initial + fallback:
                store.add(value)
            store.close()

            select_calls = 0

            def select_candidates(*_args, **_kwargs):
                nonlocal select_calls
                select_calls += 1
                return (initial if select_calls == 1 else fallback), {}, {}

            def ready(_inventory, accepted, _now, _rules, _past, _limit):
                items = list(accepted.values())
                return items, {item.article_id: {"channel": "technology"} for item in items}, {}

            repair_issued = False

            def validate(_payload, values, _model, generated_at):
                nonlocal repair_issued
                results = []
                for value in values:
                    if repair and not repair_issued and value.id == initial[0].id:
                        repair_issued = True
                        problem = BilingualValidationError("hard_facts", "repair one title", ("title_cn",), ("title_cn",))
                        results.append(ItemValidation(value, {"id": value.id, "title_cn": "bad"}, None, problem))
                    else:
                        results.append(ItemValidation(value, {}, enrichment(value, generated_at), None))
                return results

            def complete(*, system_prompt, user_prompt, max_tokens):
                if system_prompt.startswith("You repair only"):
                    return json.dumps({"id": initial[0].id, "fields": {"title_cn": "正确标题"}}), {
                        "prompt_tokens": 4_300, "completion_tokens": 700, "total_tokens": 5_000,
                    }, "deepseek-test"
                payload = json.loads(user_prompt.split("\n", 1)[1])
                values = payload["candidates"]
                output = min(4_000, max_tokens)
                input_count = per_batch_tokens - output
                return json.dumps({"items": [{"id": value["id"]} for value in values]}), {
                    "prompt_tokens": input_count, "completion_tokens": output, "total_tokens": per_batch_tokens,
                }, "deepseek-test"

            env = {
                "MAX_BATCH_ARTICLES": "6", "MAX_NEWS_INPUT_CHARS_PER_ARTICLE": "1200",
                "MAX_NEWS_OUTPUT_TOKENS": "7200", "MAX_DAILY_TOKENS": str(configured_budget),
            }
            with patch.dict("os.environ", env), \
                 patch("ai_daily_pipeline.enrich.select", side_effect=select_candidates), \
                 patch("ai_daily_pipeline.enrich._ready_selection", side_effect=ready), \
                 patch("ai_daily_pipeline.enrich._validated_batch", side_effect=validate), \
                 patch("ai_daily_pipeline.enrich._build_enrichment", side_effect=lambda _item, value, _model, stamp: enrichment(value, stamp)), \
                 patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                client.return_value.complete_json.side_effect = complete
                result = run_enrichment(root, now=NOW, token_budget_override=override)
                calls = client.return_value.complete_json.call_count
            status = json.loads((root / "data" / "supply-status.json").read_text(encoding="utf-8"))
            output = json.loads(result.output_path.read_text(encoding="utf-8")) if result.output_path else None
            published = None
            if result.output_path:
                report_path = publish_latest_report(root, root / "site")
                published = json.loads(report_path.read_text(encoding="utf-8"))
            return result, status, output, published, calls

    def test_fourteen_stories_complete_normally(self):
        result, status, output, report, calls = self._run_case(14, 1_500)
        self.assertEqual(result.saved, 14)
        self.assertEqual(result.token_budget_status, "normal")
        self.assertEqual(status["accepted_count"], 14)
        self.assertEqual(status["batches_completed"], 3)
        self.assertEqual(report["article_count"], 14)
        self.assertEqual(report["supply"]["target_count"], 14)
        self.assertEqual(calls, 3)

    def test_one_process_override_does_not_raise_default_budget(self):
        result, status, _output, report, calls = self._run_case(14, 17_500, override=80_000)
        self.assertEqual(result.saved, 14)
        self.assertEqual(status["token_budget"], 80_000)
        self.assertEqual(status["token_used"], 52_500)
        self.assertEqual(report["article_count"], 14)
        self.assertEqual(calls, 3)
        default_result, default_status, _output, _report, default_calls = self._run_case(14, 17_500)
        self.assertEqual(default_status["token_budget"], 40_000)
        self.assertLess(default_calls, calls)

    def test_twelve_stories_publish_when_next_batch_is_unsafe(self):
        result, status, output, report, calls = self._run_case(12, 15_000)
        self.assertEqual(result.saved, 12)
        self.assertEqual(status["token_budget_status"], "graceful_stop")
        self.assertEqual(status["token_used"], 30_000)
        self.assertEqual(status["token_remaining"], 10_000)
        self.assertGreater(status["next_batch_required_tokens"], 10_000)
        self.assertEqual(report["article_count"], 12)
        self.assertEqual(report["supply"]["stopped_reason"], "token_budget_insufficient_for_next_batch")
        self.assertEqual(calls, 2)

    def test_ten_stories_publish_when_next_batch_is_unsafe(self):
        result, status, output, report, calls = self._run_case(10, 18_000)
        self.assertEqual(result.saved, 10)
        self.assertEqual(status["token_budget_status"], "graceful_stop")
        self.assertEqual(report["article_count"], 10)
        self.assertEqual(calls, 2)

    def test_environment_cannot_raise_the_daily_cap_above_forty_thousand(self):
        result, status, output, report, calls = self._run_case(12, 15_000, configured_budget=50_000)
        self.assertEqual(status["token_budget"], 40_000)
        self.assertEqual(result.token_budget_status, "graceful_stop")
        self.assertEqual(report["article_count"], 12)

    def test_nine_stories_publish_when_token_guard_stops_fallback(self):
        result, status, output, report, calls = self._run_case(9, 18_000)
        self.assertEqual(result.saved, 9)
        self.assertEqual(result.token_budget_status, "graceful_stop")
        self.assertEqual(status["daily_mode"], "graceful_degraded")
        self.assertEqual(status["accepted_count"], 9)
        self.assertEqual(status["minimum_not_met_reason"], "token_budget_exhausted")
        self.assertIsNotNone(output)
        self.assertEqual(report["article_count"], 9)
        self.assertEqual(calls, 2)

    def test_all_daily_modes_publish_only_real_qualified_items(self):
        for count, expected in ((14, "normal"), (10, "normal"), (9, "graceful_degraded"),
                                (5, "graceful_degraded"), (4, "minimal_daily"),
                                (1, "minimal_daily"), (0, "true_failure")):
            with self.subTest(count=count):
                result, status, output, report, _calls = self._run_case(count, 5_000, fallback_count=0)
                self.assertEqual(status["daily_mode"], expected)
                self.assertEqual(status["publishable_count"], count)
                self.assertEqual(status["target_count"], 14)
                self.assertEqual(result.saved, count)
                if count:
                    self.assertEqual(report["article_count"], count)
                    self.assertEqual(report["daily_mode"], expected)
                    self.assertEqual(output["supply"]["daily_mode"], expected)
                else:
                    self.assertIsNone(output)
                    self.assertIsNone(report)

    def test_seven_stories_publish_on_token_guard(self):
        result, status, output, report, calls = self._run_case(7, 18_000)
        self.assertEqual(result.saved, 7)
        self.assertEqual(status["token_budget_status"], "graceful_stop")
        self.assertTrue(status["token_guard_triggered"])
        self.assertEqual(status["daily_mode"], "graceful_degraded")
        self.assertEqual(report["article_count"], 7)
        self.assertEqual(calls, 2)

    def test_local_repair_usage_counts_before_next_batch(self):
        result, status, output, report, calls = self._run_case(10, 11_000, repair=True)
        self.assertEqual(status["token_used"], 27_000)
        self.assertEqual(status["token_budget_status"], "graceful_stop")
        self.assertEqual(status["accepted_count"], 10)
        self.assertEqual(report["article_count"], 10)
        self.assertEqual(calls, 3)  # two ordinary batches plus one field-only repair


if __name__ == "__main__":
    unittest.main()

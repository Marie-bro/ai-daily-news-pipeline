from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from ai_daily_pipeline.enrich import _budget_status
from ai_daily_pipeline.extract import ExtractedArticle
from ai_daily_pipeline.models import Article, SourceItem
from ai_daily_pipeline.pipeline import _candidate_to_article, run_collection
from ai_daily_pipeline.run_audit import RunAudit
from ai_daily_pipeline.sources import SourceCollection, SourceDefinition
from ai_daily_pipeline.supply import DEFAULT, select
from ai_daily_pipeline.text import article_id

NOW = datetime(2026, 9, 28, 0, tzinfo=UTC)


def story(n, **values):
    base = Article(str(n), "science", f"Quantum discovery {n}", f"Quantum discovery {n}",
        "Research", "official_blog", (NOW-timedelta(hours=1)).isoformat(),
        f"https://example.com/{n}", "en", "raw", "Quantum research findings " * 80,
        f"fingerprint-{n}", NOW.isoformat(), "source_verified")
    return replace(base, **values)


class RunAuditTests(unittest.TestCase):
    def test_collection_and_inventory_are_separate_real_sets(self):
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            current = story(1)
            historical = story(2)
            item = SourceItem("source", "Research", "official_blog", current.title,
                              current.original_url, NOW, 1)
            audit.metrics["raw_index_entries_seen"] = 3
            audit.collection_sets([item, item], [current], [current.id])
            audit.event(current, "initial_candidate_pool", "kept", "eligible")
            audit.inventory_sets([current, historical], [current, historical], [historical])
            audit.enrichment_sets([historical])
            audit.model_submitted([historical], 1)
            flows = json.loads(audit.save().read_text(encoding="utf-8"))["flows"]
            self.assertEqual(flows["collection"]["raw_index"]["count"], 3)
            self.assertEqual(flows["collection"]["source_item"]["count"], 2)
            self.assertEqual(flows["collection"]["source_item"]["distinct_article_ids"], [article_id(current.original_url)])
            self.assertEqual(flows["collection"]["current_run_article"]["article_ids"], [current.id])
            self.assertEqual(flows["inventory_candidate"]["historical_inventory"]["article_ids"], [historical.id])
            self.assertEqual(flows["inventory_candidate"]["candidate_pool"]["article_ids"], [current.id])
            self.assertEqual(flows["inventory_candidate"]["selected_candidate"]["article_ids"], [historical.id])
            self.assertEqual(flows["inventory_candidate"]["model_submitted"]["batches"][0]["article_ids"], [historical.id])

    def test_inventory_set_accounting_preserves_later_drop_reason(self):
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            current = story(1)
            audit.collection_sets([], [current], [])
            audit.inventory_origin_events([current])
            audit.event(current, "content_gate", "dropped", "blocked_content_terms")
            audit.inventory_sets([current], [], [])
            trace = audit.traces[current.id]
            self.assertEqual(trace["final_status"], "dropped")
            self.assertEqual(trace["final_reason"], "blocked_content_terms")
            self.assertEqual([event["stage"] for event in trace["events"]], ["inventory", "content_gate"])

    def test_keep_drop_trace_and_atomic_readback(self):
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            item = story(1)
            audit.event(item, "collection", "kept", "parsed_source_item")
            audit.event(item, "freshness", "kept", "level_1")
            audit.event(item, "publishable", "dropped", "enriched_importance_below_60")
            result = json.loads(audit.save("failed").read_text(encoding="utf-8"))
            self.assertEqual(result["articles"][0]["article_id"], "1")
            self.assertEqual(result["articles"][0]["final_reason"], "enriched_importance_below_60")
            self.assertEqual([x["status"] for x in result["articles"][0]["events"]], ["kept", "kept", "dropped"])

    def test_historical_duplicate_reason_is_specific(self):
        article = story(1)
        past = [{"original_url": "https://unrelated.example/1", "fingerprint": article.fingerprint, "title_original": "Completely different news"}]
        events = []
        selected, _, _ = select([article], NOW, DEFAULT, past,
                                 on_decision=lambda *args: events.append(args))
        self.assertEqual(selected, [])
        self.assertIn(("historical_dedup", "dropped", "historical_fingerprint"), [(x[1], x[2], x[3]) for x in events])

    def test_duplicate_url_in_same_selection(self):
        values = [story(1), story(2, original_url="https://example.com/1", title="Different issue")]
        events = []
        selected, _, _ = select(values, NOW, DEFAULT,
                                 on_decision=lambda *args: events.append(args))
        self.assertEqual(len(selected), 1)
        self.assertTrue(any(x[1:4] == ("selection", "dropped", "duplicate_url") for x in events))

    def test_final_publishable_score_filter(self):
        a = story(1)
        b = story(2)
        enrichments = {"1": type("E", (), {"importance_score": 59})(), "2": type("E", (), {"importance_score": 60})()}
        events = []
        selected, _, _ = select([a, b], NOW, DEFAULT, enrichments=enrichments,
                                 on_decision=lambda *args: events.append(args))
        self.assertEqual([x.id for x in selected], ["2"])
        self.assertTrue(any(x[0].id == "1" and x[3] == "enriched_importance_below_60" for x in events))

    def test_validation_failure_trace_is_distinct_from_token_stop(self):
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            audit.event(story(1), "validation", "dropped", "hard_facts")
            audit.event(story(2), "token_budget", "dropped", "graceful_stop")
            result = json.loads(audit.save().read_text(encoding="utf-8"))
            self.assertEqual({(x["stage"], x["reason"]) for x in result["drop_reasons"]},
                             {("validation", "hard_facts"), ("token_budget", "graceful_stop")})
            self.assertEqual(_budget_status(10, 35730, 40000, 6000, 10), "graceful_stop")

    def test_single_article_rejection_has_no_fetch_or_model_side_effect(self):
        src = SourceDefinition("test", "Research", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1)
        item = SourceItem("test", "Research", "official_blog", "Quantum short", "https://example.com/short", NOW, 1)
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            with patch("ai_daily_pipeline.pipeline.fetch", return_value="<p>short</p>"), \
                 patch("ai_daily_pipeline.pipeline.extract_article", return_value=ExtractedArticle("Quantum short", "short", "short", NOW)):
                self.assertIsNone(_candidate_to_article(item, NOW, src, audit))
            self.assertEqual(audit.traces[next(iter(audit.traces))]["final_reason"], "content_shorter_than_120")

    def test_one_source_failure_is_isolated_and_recorded(self):
        src = SourceDefinition("broken", "Broken", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit = RunAudit(root, NOW)
            with patch("ai_daily_pipeline.pipeline.load_sources", return_value=[src]), \
                 patch("ai_daily_pipeline.pipeline.collect_source", side_effect=TimeoutError("source timeout")):
                result = run_collection(root, True, NOW, audit=audit)
            self.assertEqual(result.accepted, 0)
            self.assertEqual(len(result.source_errors), 1)
            self.assertEqual(audit.sources[0]["status"], "error")

    def test_cached_enrichment_final_audit_does_not_call_model(self):
        from ai_daily_pipeline.enrich import _validated_enrichments, run_enrichment
        from ai_daily_pipeline.store import ArticleStore
        from test_enrich import article, model_item
        with TemporaryDirectory() as directory:
            root = Path(directory)
            value = article()
            store = ArticleStore(root / "data/ai_daily.sqlite3")
            store.add(value)
            enriched = _validated_enrichments({"items": [model_item()]}, [value], "test", value.created_at)[0]
            store.save_enrichment(enriched)
            store.commit()
            store.close()
            audit = RunAudit(root, datetime.fromisoformat(value.created_at))
            with patch("ai_daily_pipeline.enrich.DeepSeekClient") as client:
                result = run_enrichment(root, now=datetime.fromisoformat(value.created_at), audit=audit)
            client.assert_not_called()
            self.assertEqual(result.saved, 1)
            trace = audit.traces[value.id]
            self.assertTrue(any(e["stage"] == "publishable" and e["status"] == "kept" for e in trace["events"]))
            self.assertEqual(audit.metrics["final_publishable"], 1)

    def test_rss_adapter_records_missing_title_and_url_without_changing_items(self):
        from ai_daily_pipeline.sources import ADAPTERS
        source = SourceDefinition("feed", "Research", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1)
        body = "<rss><channel><item><title>Quantum discovery</title><link>https://example.com/one</link></item><item><title></title><link>https://example.com/two</link></item><item><title>Quantum update</title></item></channel></rss>"
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            original = ADAPTERS["rss"].parse(source, body)
            observed = ADAPTERS["rss"].parse(source, body, audit.parser_event)
            self.assertEqual(original, observed)
            reasons = {e["reason"] for trace in audit.traces.values() for e in trace["events"]}
            self.assertIn("parsed_feed_item", reasons)
            self.assertIn("missing_title", reasons)
            self.assertIn("missing_url", reasons)
            self.assertEqual(audit.metrics["raw_index_entries_seen"], 3)

    def test_json_adapter_records_existing_filter_reasons(self):
        from ai_daily_pipeline.sources import ADAPTERS
        source = SourceDefinition("json", "Research", "official_blog", "json_index", "https://example.com/feed", ("example.com",), 1)
        body = json.dumps([{"title": "Quantum research breakthrough", "url": "https://example.com/one"},
                           {"title": "Unrelated headline", "url": "https://example.com/two"},
                           {"title": "AI software release", "url": "http://example.com/three"},
                           {"title": "AI missing link"}, "broken row"])
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            original = ADAPTERS["json_index"].parse(source, body)
            observed = ADAPTERS["json_index"].parse(source, body, audit.parser_event)
            self.assertEqual(original, observed)
            reasons = {e["reason"] for trace in audit.traces.values() for e in trace["events"]}
            self.assertTrue({"parsed_json_item", "radar_terms_absent", "invalid_or_disallowed_url", "missing_url", "malformed_article"} <= reasons)

    def test_json_index_limit_is_a_recorded_drop_without_changing_output(self):
        from ai_daily_pipeline.sources import ADAPTERS
        source = SourceDefinition("json", "Research", "official_blog", "json_index", "https://example.com/feed", ("example.com",), 1)
        body = json.dumps([{"title": f"Quantum research discovery {n}", "url": f"https://example.com/{n}"}
                           for n in range(101)])
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            original = ADAPTERS["json_index"].parse(source, body)
            observed = ADAPTERS["json_index"].parse(source, body, audit.parser_event)
            self.assertEqual(original, observed)
            self.assertEqual(len(observed), 100)
            self.assertEqual(audit.metrics["raw_index_entries_seen"], 101)
            self.assertEqual(sum(any(e["reason"] == "index_cap_100" for e in trace["events"])
                                 for trace in audit.traces.values()), 1)

    def test_html_adapter_records_title_and_path_rejections_without_changing_output(self):
        from ai_daily_pipeline.sources import ADAPTERS
        source = SourceDefinition("html", "Research", "official_blog", "html_index", "https://example.com/news", ("example.com",), 1,
                                  article_path_pattern=r"/article/")
        body = ('<a href="/article/1">Quantum research discovery published today</a>'
                '<a href="/archive">AI software product release explained</a>'
                '<a href="/article/2">short</a>')
        with TemporaryDirectory() as directory:
            audit = RunAudit(Path(directory), NOW)
            original = ADAPTERS["html_index"].parse(source, body)
            observed = ADAPTERS["html_index"].parse(source, body, audit.parser_event)
            self.assertEqual(original, observed)
            reasons = {e["reason"] for trace in audit.traces.values() for e in trace["events"]}
            self.assertIn("parsed_index_link", reasons)
            self.assertIn("article_path_pattern_mismatch", reasons)
            self.assertIn("title_shorter_than_12", reasons)

    def test_source_cap_preserves_a_drop_for_each_article(self):
        src = SourceDefinition("s", "Research", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1)
        items = tuple(SourceItem("s", "Research", "official_blog", f"Quantum {n}", f"https://example.com/{n}", NOW, 1) for n in range(22))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit = RunAudit(root, NOW)
            with patch("ai_daily_pipeline.pipeline.load_sources", return_value=[src]), \
                 patch("ai_daily_pipeline.pipeline.collect_source", return_value=SourceCollection(items, "ok")), \
                 patch("ai_daily_pipeline.pipeline._candidate_to_article", return_value=None):
                run_collection(root, True, NOW, audit=audit)
            self.assertEqual(sum(any(e["reason"] == "per_source_scan_cap_20" for e in x["events"]) for x in audit.traces.values()), 2)


if __name__ == "__main__":
    unittest.main()

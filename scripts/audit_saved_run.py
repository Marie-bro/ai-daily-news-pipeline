"""Offline retrospective from already saved data. No source fetch, model, publication or send."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from ai_daily_pipeline.enrich import _eligible_content, _dedupe_events
from ai_daily_pipeline.models import Article, Enrichment
from ai_daily_pipeline.run_audit import write_viewer
from ai_daily_pipeline.sources import load_sources
from ai_daily_pipeline.supply import history, policy, select

UNKNOWN = "not_observable_from_existing_data"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def build(root: Path, date: str):
    raw = read_json(root / "data/latest-run.json")
    bilingual = read_json(root / "data/bilingual-validation-audit.json")
    at = datetime.fromisoformat(bilingual["generated_at"])
    if at.date().isoformat() != date or not raw.get("articles") or any(
        x["created_at"][:10] != date for x in raw["articles"]
    ):
        raise ValueError("saved latest-run and bilingual audit do not both belong to the requested date")
    if len(bilingual["entries"]) != len({x["article_id"] for x in bilingual["entries"]}):
        raise ValueError("duplicate model audit entries; cannot assign a unique outcome")
    db = root / "data/ai_daily.sqlite3"
    uri = db.resolve().as_uri().replace("file:///", "file:/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        inventory = [Article(**dict(x)) for x in connection.execute(
            "SELECT * FROM articles WHERE published_at >= ? AND published_at <= ? AND created_at <= ? ORDER BY published_at DESC",
            ((at - timedelta(hours=168)).isoformat(), (at + timedelta(minutes=10)).isoformat(), at.isoformat()))]
        enriched_before = {x["article_id"]: Enrichment(**dict(x)) for x in connection.execute(
            "SELECT * FROM tech_enrichments WHERE generated_at < ?", (at.isoformat(),))}
        enriched_final = {x["article_id"]: Enrichment(**dict(x)) for x in connection.execute(
            "SELECT * FROM tech_enrichments WHERE generated_at <= ?", (at.isoformat(),))}
        usage_rows = [dict(x) for x in connection.execute(
            "SELECT task,model,created_at,input_tokens,output_tokens,total_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens FROM model_usage WHERE created_at LIKE ?", (date + "%",))]
        stored_rows = {x["id"]: dict(x) for x in connection.execute("SELECT id,created_at,original_url,fingerprint FROM articles")}
    eligible = [a for a in inventory if _eligible_content(a)]
    past = history(root.parent / "ai-daily-public-site", at)
    rules = policy(root)
    initial_events = {}
    initial, _, _ = select(eligible, at, rules, past, enriched_before, limit=rules["maximum"],
        on_decision=lambda a, stage, status, reason, detail: initial_events.setdefault(a.id, []).append(
            {"stage": "initial_" + stage, "status": status, "reason": reason, "detail": detail}))
    ready = [replace(a, category=enriched_final[a.id].category) for a in eligible if a.id in enriched_final]
    final_events = {}
    chosen, _, _ = select(ready, at, rules, past, enriched_final, limit=rules["maximum"],
        on_decision=lambda a, stage, status, reason, detail: final_events.setdefault(a.id, []).append(
            {"stage": "final_" + stage, "status": status, "reason": reason, "detail": detail}))
    final = _dedupe_events([enriched_final[a.id] for a in chosen])
    final_ids = {e.article_id for e in final}
    model = {x["article_id"]: x for x in bilingual["entries"]}
    blocked_batch_ids = set((bilingual.get("failure") or {}).get("batch_article_ids") or [])
    sources_by_name = {}
    for source in load_sources(root / "config/sources.json"):
        sources_by_name.setdefault(source.name, []).append(source.source_id)
    collected = {x["id"] for x in raw["articles"]}
    items = []
    for original in raw["articles"]:
        ident = original["id"]
        audit = model.get(ident)
        events = [{"stage": "collection", "status": "kept", "reason": "saved_collection_article", "detail": {}}]
        events += initial_events.get(ident, [])
        if audit:
            events.append({"stage": "enrichment", "status": "kept", "reason": "model_batch_requested", "detail": {}})
            events.append({"stage": "validation", "status": "kept" if audit["status"] == "accepted" else "dropped",
                           "reason": "accepted" if audit["status"] == "accepted" else audit["reject_stage"] or "validation_rejected",
                           "detail": {"reject_reason": audit["reject_reason"]}})
        elif ident in blocked_batch_ids:
            events.append({"stage": "token_budget", "status": "dropped", "reason": "batch_not_submitted_token_guard", "detail": {}})
        else:
            events.append({"stage": "enrichment", "status": "not_reached", "reason": "not_requested_before_budget_stop", "detail": {}})
        events += final_events.get(ident, [])
        if ident in final_ids:
            events.append({"stage": "publishable", "status": "kept", "reason": "offline_selector_reconstruction", "detail": {}})
        elif audit and audit["status"] == "accepted":
            event = next((x for x in final_events.get(ident, []) if x["status"] == "dropped"), None)
            events.append({"stage": "publishable", "status": "dropped", "reason": event["reason"] if event else UNKNOWN,
                           "detail": event["detail"] if event else {}})
        ids = sources_by_name.get(original["source"], [])
        items.append({"article_id": ident, "trace_id": ident, "source": original["source"],
                      "source_id": UNKNOWN, "source_id_inferred_from_current_config": ids[0] if len(ids) == 1 else UNKNOWN,
                      "original_url": original["original_url"],
                      "title": original["title"], "published_at": original["published_at"],
                      "collection": "kept", "normalization": UNKNOWN, "freshness": UNKNOWN,
                      "dedup": UNKNOWN, "candidate": "selected_initial" if ident in {a.id for a in initial} else UNKNOWN,
                      "enrichment": audit["status"] if audit else "batch_not_submitted" if ident in blocked_batch_ids else "not_requested",
                      "validation": audit["reject_stage"] if audit and audit["status"] == "rejected" else "passed" if audit else "not_requested",
                      "publishable": "offline_selected" if ident in final_ids else "offline_excluded" if audit and audit["status"] == "accepted" else "not_reached",
                      "current_stage": events[-1]["stage"],
                      "screening_status": "publishable_candidate" if ident in final_ids else
                                          "validation_failed" if audit and audit["status"] == "rejected" else
                                          "final_selector_excluded" if audit and audit["status"] == "accepted" else
                                          "batch_not_submitted" if ident in blocked_batch_ids else "not_reached",
                      "screening_reason": "offline_selected" if ident in final_ids else
                                          audit["reject_stage"] if audit and audit["status"] == "rejected" else
                                          next((e["reason"] for e in final_events.get(ident, []) if e["status"] == "dropped"), UNKNOWN) if audit and audit["status"] == "accepted" else
                                          "token_budget_guard" if ident in blocked_batch_ids else "not_processed_before_budget_stop",
                      "final_status": "not_published", "final_reason": "run_failed_token_budget",
                      "events": events})
    accepted = [x for x in bilingual["entries"] if x["status"] == "accepted"]
    accepted_details = []
    for entry in accepted:
        ident = entry["article_id"]
        enriched = enriched_final.get(ident)
        event = next((x for x in final_events.get(ident, []) if x["status"] == "dropped"), None)
        why = "offline_selected" if ident in final_ids else event["reason"] if event else UNKNOWN
        accepted_details.append({"article_id": ident, "source": entry["source"],
                                 "title_en": enriched.title_en if enriched else UNKNOWN,
                                 "title_cn": enriched.title_cn if enriched else UNKNOWN,
                                 "original_url": enriched.original_url if enriched else UNKNOWN,
                                 "importance_score": enriched.importance_score if enriched else UNKNOWN,
                                 "from_107_collected": ident in collected,
                                 "offline_publishable": ident in final_ids,
                                 "reason": why, "events": final_events.get(ident, [])})
    status = bilingual.get("failure") or {}
    source_statuses = Counter(s["status"] for s in raw["source_health"])
    result = {
        "run_id": "offline-" + date, "date": date, "start_time": UNKNOWN,
        "end_time": bilingual["generated_at"], "mode": "saved_data_read_only_reconstruction",
        "observability": "final per-item selection was not saved in the historical run; outcomes below replay current deterministic selector against saved SQLite and past report files",
        "sources_attempted": len(raw["source_health"]), "sources_ok": source_statuses["ok"],
        "sources_empty": source_statuses["empty"], "sources_failed": source_statuses["error"],
        "raw_articles": raw["fetched_source_items"], "normalized_articles": UNKNOWN,
        "freshness_passed": UNKNOWN, "dedup_passed": UNKNOWN,
        "collection_accepted": raw["accepted"], "collection_inserted": raw["inserted"],
        "candidate_pool": UNKNOWN, "initial_selected": len(initial),
        "enrichment_requested": len(model), "enrichment_succeeded": len(accepted),
        "enrichment_failed": len(model) - len(accepted), "validation_passed": len(accepted),
        "validation_failed": len(model) - len(accepted),
        "repair_attempted": sum(bool(x.get("repair_attempted") or x.get("repaired")) for x in bilingual["entries"]),
        "repair_succeeded": sum(bool(x.get("repaired")) for x in bilingual["entries"]),
        "final_publishable_offline": len(final), "actual_published": 0,
        "target_count": rules["target"], "min_count": rules["minimum"], "max_count": rules["maximum"],
        "token_used": sum(int(x["total_tokens"] or 0) for x in usage_rows),
        "token_remaining": max(0, 40000 - sum(int(x["total_tokens"] or 0) for x in usage_rows)),
        "final_status": "failed_before_publication", "failure": status,
        "normalization_errors": raw["normalization_errors"],
        "funnel": [
            {"stage": "sources", "count": len(raw["source_health"]), "observed": True},
            {"stage": "parsed_source_items", "count": raw["fetched_source_items"], "observed": True},
            {"stage": "normalized", "count": UNKNOWN, "observed": False},
            {"stage": "passed_feed_and_extracted_freshness", "count": UNKNOWN, "observed": False},
            {"stage": "after_dedup", "count": UNKNOWN, "observed": False},
            {"stage": "collection_accepted", "count": raw["accepted"], "observed": True},
            {"stage": "initial_selected_for_enrichment", "count": len(initial), "observed": False},
            {"stage": "model_attempted_across_initial_and_fallback", "count": len(model), "observed": True},
            {"stage": "model_accepted", "count": len(accepted), "observed": True},
            {"stage": "theoretical_final_publishable", "count": len(final), "observed": False},
            {"stage": "actually_published", "count": 0, "observed": True},
        ], "accepted_13": accepted_details, "articles_107": items,
        "drop_reasons": [
            {"stage": "normalization", "reason": reason, "count": count, "observed": True}
            for reason, count in sorted(Counter(e["error_kind"] for e in raw["normalization_errors"]).items())
        ] + [
            {"stage": "validation", "reason": reason, "count": count, "observed": True}
            for reason, count in sorted(Counter(e["reject_stage"] or "schema_invalid" for e in bilingual["entries"] if e["status"] == "rejected").items())
        ] + [
            {"stage": "final_publishable", "reason": reason, "count": count, "observed": False}
            for reason, count in sorted(Counter(x["reason"] for x in accepted_details if not x["offline_publishable"]).items())
        ] + [{"stage": "token_budget", "reason": "batch_not_submitted", "count": len(blocked_batch_ids), "observed": True}],
        "requested_from_107": sum(x["article_id"] in collected for x in bilingual["entries"]),
        "blocked_next_batch_from_107": sum(ident in collected for ident in blocked_batch_ids),
        "accepted_from_107": sum(x["article_id"] in collected for x in accepted),
        "usage_rows": usage_rows,
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = build(args.root.resolve(), args.date)
    path = args.root.resolve() / "data/run-audits" / ("offline-" + args.date + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_viewer(path, result)
    print(f"offline audit saved: {path}")
    print({key: result[key] for key in ("raw_articles", "collection_accepted", "initial_selected", "enrichment_requested", "enrichment_succeeded", "final_publishable_offline", "actual_published")})

if __name__ == "__main__":
    main()

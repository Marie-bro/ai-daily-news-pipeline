"""Run isolated historical bilingual replays without publishing a report or sending Feishu."""
from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from ai_daily_pipeline.deepseek import DeepSeekError
from ai_daily_pipeline.enrich import EnrichmentError, run_enrichment
from ai_daily_pipeline.models import Article
from ai_daily_pipeline.store import ArticleStore


def _copy_config(source: Path, destination: Path) -> None:
    shutil.copytree(source / "config", destination / "config")


def _usage_for_day(store: ArticleStore, day: str) -> dict[str, int]:
    rows = [row for row in store.usage_rows() if str(row["created_at"]).startswith(day)]
    return {
        "calls": len(rows),
        "input_tokens": sum(int(row["input_tokens"] or 0) for row in rows),
        "output_tokens": sum(int(row["output_tokens"] or 0) for row in rows),
        "total_tokens": sum(int(row["total_tokens"] or 0) for row in rows),
        "cache_hit_tokens": sum(int(row["prompt_cache_hit_tokens"] or 0) for row in rows),
        "cache_miss_tokens": sum(int(row["prompt_cache_miss_tokens"] or 0) for row in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="captured source candidates JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--end", default="2026-09-27T00:00:00+00:00")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--max-articles", type=int, default=8)
    parser.add_argument("--input-characters", type=int, default=1600)
    parser.add_argument("--output-tokens", type=int, default=4200)
    parser.add_argument("--audit-root", type=Path, help="optional ignored directory for isolated replay evidence")
    args = parser.parse_args()
    source_root = Path(__file__).resolve().parent
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    names = {item.name for item in fields(Article)}
    articles = [Article(**{key: value for key, value in item.items() if key in names}) for item in raw["articles"]]
    end = datetime.fromisoformat(args.end).astimezone(UTC)

    # The replay has a separate SQLite, report history, and output directory. No publisher or delivery code is invoked.
    previous = {key: os.environ.get(key) for key in (
        "MAX_BATCH_ARTICLES", "MAX_NEWS_INPUT_CHARS_PER_ARTICLE", "MAX_NEWS_OUTPUT_TOKENS",
        "MAX_BILINGUAL_REPAIR_OUTPUT_TOKENS", "MAX_DAILY_TOKENS",
    )}
    os.environ.update({
        "MAX_BATCH_ARTICLES": str(args.max_articles),
        "MAX_NEWS_INPUT_CHARS_PER_ARTICLE": str(args.input_characters),
        "MAX_NEWS_OUTPUT_TOKENS": str(args.output_tokens),
        "MAX_BILINGUAL_REPAIR_OUTPUT_TOKENS": "800",
        "MAX_DAILY_TOKENS": "40000",
    })
    try:
        with TemporaryDirectory(prefix="mariespace-bilingual-replay-") as temporary:
            sandbox = Path(temporary)
            pipeline_root = sandbox / "pipeline"
            site_root = sandbox / "ai-daily-public-site"
            (pipeline_root / "data").mkdir(parents=True)
            _copy_config(source_root, pipeline_root)
            store = ArticleStore(pipeline_root / "data" / "ai_daily.sqlite3")
            try:
                for article in articles:
                    store.add(article)
            finally:
                store.close()

            report_days: list[dict[str, object]] = []
            for offset in reversed(range(args.days)):
                run_at = end - timedelta(days=offset)
                date = run_at.date().isoformat()
                row: dict[str, object] = {"date": date, "published": False, "feishu_sent": False}
                try:
                    result = run_enrichment(pipeline_root, now=run_at)
                    audit_file = pipeline_root / "data" / "bilingual-validation-audit.json"
                    audit = json.loads(audit_file.read_text(encoding="utf-8")) if audit_file.exists() else {"summary": {}}
                    supply = json.loads((pipeline_root / "data" / "supply-status.json").read_text(encoding="utf-8"))
                    usage_store = ArticleStore(pipeline_root / "data" / "ai_daily.sqlite3")
                    try:
                        token_usage = _usage_for_day(usage_store, date)
                    finally:
                        usage_store.close()
                    row.update({
                        "initial_candidates": audit["summary"].get("initial_candidates", 0),
                        "fact_schema_generated": audit["summary"].get("fact_schema_generated", 0),
                        "model_accepted": audit["summary"].get("model_accepted", 0),
                        "hard_fact_rejected": audit["summary"].get("hard_fact_rejected", 0),
                        "semantic_rejected": audit["summary"].get("semantic_rejected", 0),
                        "repaired": audit["summary"].get("repaired", 0),
                        "fallback_added": audit["summary"].get("fallback_added", 0),
                        "final_count": result.saved,
                        "minimum_met": audit["summary"].get("minimum_met", False),
                        "minimum_not_met_reason": supply.get("minimum_not_met_reason"),
                        "token_usage": token_usage,
                    })
                    if result.output_path and result.output_path.exists():
                        digest = json.loads(result.output_path.read_text(encoding="utf-8"))
                        report_path = site_root / "data" / "daily" / "ai" / f"{date}.json"
                        report_path.parent.mkdir(parents=True, exist_ok=True)
                        report_path.write_text(json.dumps({"report_date": date, "items": digest["items"]}, ensure_ascii=False), encoding="utf-8")
                except (DeepSeekError, EnrichmentError, OSError, json.JSONDecodeError) as exc:
                    row["error"] = str(exc)
                    usage_store = ArticleStore(pipeline_root / "data" / "ai_daily.sqlite3")
                    try:
                        row["token_usage"] = _usage_for_day(usage_store, date)
                    finally:
                        usage_store.close()
                report_days.append(row)
            if args.audit_root:
                args.audit_root.mkdir(parents=True, exist_ok=True)
                shutil.copytree(pipeline_root / "data", args.audit_root / "pipeline-data", dirs_exist_ok=True)
                if site_root.exists():
                    shutil.copytree(site_root, args.audit_root / "site-history", dirs_exist_ok=True)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "mode": "isolated real-model historical replay",
        "source_snapshot": str(args.input),
        "limitation": "The candidate pages are a captured current-source snapshot filtered by original publication time; they are not immutable historical page snapshots.",
        "published": False,
        "feishu_sent": False,
        "days": report_days,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "days": report_days}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

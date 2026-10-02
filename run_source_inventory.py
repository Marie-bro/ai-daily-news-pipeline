from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import time
from pathlib import Path

from ai_daily_pipeline.sources import collect_source, load_sources
from ai_daily_pipeline.store import ArticleStore
from ai_daily_pipeline.diagnostics import error_kind


def main() -> int:
    parser = argparse.ArgumentParser(description="Report effective MarieSpace Radar source metadata and runtime health.")
    parser.add_argument("--live", action="store_true", help="Fetch every enabled source index before reporting.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sources = [source for source in load_sources(root / "config" / "sources.json") if source.enabled]
    store = ArticleStore(root / "data" / "ai_daily.sqlite3")
    try:
        if args.live:
            checked_at = datetime.now(UTC).isoformat()

            def check(source):
                started = time.monotonic()
                try:
                    result = collect_source(source)
                    return source.source_id, result.status, len(result.items), None, (time.monotonic()-started)*1000, None
                except Exception as exc:
                    return source.source_id, "error", 0, f"{type(exc).__name__}: {exc}", (time.monotonic()-started)*1000, error_kind(exc)

            with ThreadPoolExecutor(max_workers=6) as executor:
                checks = list(executor.map(check, sources))
            for source_id, status, items, error, duration, kind in checks:
                store.record_source_health(source_id, status, checked_at, items, error, duration_ms=duration, error_kind=kind)
        health = store.source_health_rows()
    finally:
        store.close()
    rows = []
    for source in sources:
        runtime = health.get(source.source_id, {})
        rows.append({
            "id": source.source_id, "name": source.name, "region": source.region,
            "source_role": source.source_role, "tier": source.tier, "language": source.language,
            "channel": list(source.channels), "category": list(source.categories),
            "fetch_method": source.fetch_method, "health_status": runtime.get("status", "not_checked"),
            "latest_success_at": runtime.get("latest_success_at"), "latest_checked_at": runtime.get("checked_at"),
            "items": runtime.get("items", 0), "error": runtime.get("error_summary"),
            "proxy_mode": source.proxy_mode,
            "request_timeout_seconds": source.request_timeout_seconds, "request_attempts": source.request_attempts,
            "health_metrics": {key: runtime.get(key) for key in (
                "success_count", "timeout_count", "empty_count", "failure_count", "average_duration_ms",
                "p95_duration_ms", "last_success_at", "consecutive_failures", "sample_count", "duration_sample_count", "statistics_since")},
        })
    payload = {
        "generated_at": datetime.now(UTC).isoformat(), "enabled": len(rows),
        "healthy": sum(row["health_status"] == "ok" for row in rows),
        "failed": sum(row["health_status"] == "error" for row in rows),
        "empty": sum(row["health_status"] == "empty" for row in rows),
        "sources": rows,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

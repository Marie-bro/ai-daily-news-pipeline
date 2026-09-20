from __future__ import annotations

import argparse
from pathlib import Path

from ai_daily_pipeline.pipeline import check_sources, run_collection
from ai_daily_pipeline.sources import load_sources


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect and standardize real technology news without model calls.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and normalize but do not write articles to SQLite.")
    parser.add_argument("--validate-sources", action="store_true", help="Validate source JSON without network access.")
    parser.add_argument("--check-sources", action="store_true", help="Check each source index and one article sample without writing data.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.validate_sources:
        sources = load_sources(root / "config" / "sources.json")
        print(f"valid sources={len(sources)} enabled={sum(source.enabled for source in sources)}")
        raise SystemExit(0)
    if args.check_sources:
        checks = check_sources(root)
        for check in checks:
            print(f"{check['source_id']}: {check['status']} items={check['items']} {check.get('error', '')}".strip())
        raise SystemExit(1 if any(check["status"] in {"error", "degraded"} for check in checks) else 0)
    result = run_collection(root, dry_run=args.dry_run)
    print(f"sources={result.fetched_source_items} accepted={result.accepted} inserted={result.inserted} window={result.time_window_hours}h errors={len(result.errors)}")

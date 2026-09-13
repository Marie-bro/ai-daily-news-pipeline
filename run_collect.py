from __future__ import annotations

import argparse
from pathlib import Path

from ai_daily_pipeline.pipeline import run_collection


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect and standardize real AI news without model calls.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and normalize but do not write articles to SQLite.")
    args = parser.parse_args()
    result = run_collection(Path(__file__).resolve().parent, dry_run=args.dry_run)
    print(f"sources={result.fetched_source_items} accepted={result.accepted} inserted={result.inserted} window={result.time_window_hours}h errors={len(result.errors)}")

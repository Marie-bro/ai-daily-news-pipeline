from __future__ import annotations

import argparse
from pathlib import Path

from ai_daily_pipeline.bilingual_backfill import BilingualBackfillError, backfill_report
from ai_daily_pipeline.deepseek import DeepSeekError


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill one historical daily report with compact English-first bilingual fields.")
    parser.add_argument("report_date", help="Existing YYYY-MM-DD report date")
    parser.add_argument("--dry-run", action="store_true", help="Build the bounded backfill prompt without calling DeepSeek or changing the report")
    args = parser.parse_args()
    pipeline_root = Path(__file__).resolve().parent
    site_root = pipeline_root.parent / "ai-daily-public-site"
    try:
        result = backfill_report(pipeline_root, site_root, args.report_date, dry_run=args.dry_run)
    except (BilingualBackfillError, DeepSeekError) as exc:
        raise SystemExit(f"bilingual backfill failed: {exc}") from exc
    print(result)

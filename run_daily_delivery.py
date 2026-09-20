from __future__ import annotations

import argparse
from pathlib import Path

from ai_daily_pipeline.delivery import DeliveryError, run_scheduled_delivery, send_existing_report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 6: deliver a verified Tech Daily to Feishu without extra model calls.")
    parser.add_argument("--scheduled", action="store_true", help="Run collection, enrichment, publication, deploy verification, then delivery.")
    parser.add_argument("--report-date", help="Send an already published report for an explicit manual verification date.")
    parser.add_argument("--site-root", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Build and validate delivery input without network or Feishu sending.")
    parser.add_argument("--send-dry-run", action="store_true", help="Build the Feishu card without sending it.")
    parser.add_argument("--verify-url", action="store_true", help="Verify the formal daily page and data without sending it.")
    parser.add_argument("--force", action="store_true", help="Explicitly allow a manual resend; scheduled runs are otherwise idempotent.")
    args = parser.parse_args()
    if args.scheduled == bool(args.report_date):
        parser.error("provide exactly one of --scheduled or --report-date YYYY-MM-DD")
    if args.force and args.scheduled:
        parser.error("--force is only available for an explicit manual --report-date resend")
    if args.verify_url and args.scheduled:
        parser.error("--verify-url is only available for an explicit manual --report-date check")
    root = Path(__file__).resolve().parent
    site = args.site_root or root.parent / "ai-daily-public-site"
    try:
        if args.scheduled:
            if args.dry_run or args.send_dry_run:
                parser.error("scheduled execution has no dry-run mode; use --report-date for delivery validation")
            result = run_scheduled_delivery(root, site)
        else:
            result = send_existing_report(root, site, args.report_date, force=args.force,
                                          dry_run=args.dry_run, send_dry_run=args.send_dry_run, verify_only=args.verify_url)
    except DeliveryError as exc:
        raise SystemExit(f"delivery failed: {exc}") from exc
    print(f"status={result.status} date={result.report_date} url={result.url or '-'} message_id={result.message_id or '-'} reason={result.reason or '-'} retries={result.retry_count}")

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_daily_pipeline.delivery import run_scheduled_delivery, send_existing_report
from ai_daily_pipeline.exit_status import ExitCode, delivery_exit_outcome, exception_exit_outcome


class DeliveryArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(int(ExitCode.INVALID_ARGUMENTS), f"{self.prog}: error: {message}\n")


def main(argv=None) -> int:
    parser = DeliveryArgumentParser(description="Phase 6: deliver a verified Tech Daily to Feishu without extra model calls.")
    parser.add_argument("--scheduled", action="store_true", help="Run collection, enrichment, publication, deploy verification, then delivery.")
    parser.add_argument("--token-budget-override", type=int, default=None,
                        help="Override the daily token budget for this scheduled process only.")
    parser.add_argument("--report-date", help="Send an already published report for an explicit manual verification date.")
    parser.add_argument("--site-root", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Build and validate delivery input without network or Feishu sending.")
    parser.add_argument("--send-dry-run", action="store_true", help="Build the Feishu card without sending it.")
    parser.add_argument("--verify-url", action="store_true", help="Verify the formal daily page and data without sending it.")
    parser.add_argument("--force", action="store_true", help="Explicitly allow a manual resend; scheduled runs are otherwise idempotent.")
    args = parser.parse_args(argv)
    if args.scheduled == bool(args.report_date):
        parser.error("provide exactly one of --scheduled or --report-date YYYY-MM-DD")
    if args.force and args.scheduled:
        parser.error("--force is only available for an explicit manual --report-date resend")
    if args.token_budget_override is not None and (not args.scheduled or args.token_budget_override <= 0):
        parser.error("--token-budget-override requires --scheduled and a positive value")
    if args.verify_url and args.scheduled:
        parser.error("--verify-url is only available for an explicit manual --report-date check")
    root = Path(__file__).resolve().parent
    site = args.site_root or root.parent / "ai-daily-public-site"
    try:
        if args.scheduled:
            if args.dry_run or args.send_dry_run:
                parser.error("scheduled execution has no dry-run mode; use --report-date for delivery validation")
            run_options = ({"token_budget_override": args.token_budget_override}
                           if args.token_budget_override is not None else {})
            result = run_scheduled_delivery(root, site, **run_options)
        else:
            result = send_existing_report(root, site, args.report_date, force=args.force,
                                          dry_run=args.dry_run, send_dry_run=args.send_dry_run, verify_only=args.verify_url)
    except Exception as exc:
        outcome = exception_exit_outcome(exc)
        # Unexpected exceptions can contain credentials; print only safe classification.
        print(f"delivery failed: type={type(exc).__name__} exit_code={int(outcome.code)} exit_reason={outcome.reason}", file=sys.stderr)
        return int(outcome.code)
    outcome = delivery_exit_outcome(result)
    print(f"status={result.status} date={result.report_date} url={result.url or '-'} message_id={result.message_id or '-'} reason={result.reason or '-'} retries={result.retry_count} exit_code={int(outcome.code)} exit_reason={outcome.reason}")
    return int(outcome.code)


if __name__ == "__main__":
    sys.exit(main())

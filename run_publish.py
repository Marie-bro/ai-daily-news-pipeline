from __future__ import annotations

import argparse
from pathlib import Path

from ai_daily_pipeline.publish import PublishError, publish_latest_report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Publish the latest validated AI Daily into the existing Feishu H5 site's static data.")
    parser.add_argument("--site-root", type=Path, default=None, help="Path to the existing ai-daily-public-site project.")
    args = parser.parse_args()
    pipeline_root = Path(__file__).resolve().parent
    site_root = args.site_root or pipeline_root.parent / "ai-daily-public-site"
    try:
        report_path = publish_latest_report(pipeline_root, site_root)
    except PublishError as exc:
        raise SystemExit(f"publish failed: {exc}") from exc
    print(f"published={report_path}")

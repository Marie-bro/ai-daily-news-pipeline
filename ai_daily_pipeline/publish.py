from __future__ import annotations

import json
from datetime import datetime
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from .sources import BLOCKED_CONTENT_HOSTS, BLOCKED_CONTENT_TERMS


class PublishError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"Cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublishError(f"{path.name} must contain a JSON object")
    return value


def _reading_minutes(items: list[dict[str, object]]) -> int:
    # A transparent display estimate based only on the published reading material, capped by the product target.
    characters = sum(len(str(item.get("summary_cn", ""))) + len(str(item.get("summary_en", ""))) for item in items)
    return max(5, min(10, ceil(characters / 650)))


def publish_latest_report(pipeline_root: Path, site_root: Path) -> Path:
    latest = _read_json(pipeline_root / "data" / "latest-enrichment.json")
    raw_items = latest.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise PublishError("No validated enrichment items are available to publish")
    items = [item for item in raw_items if isinstance(item, dict)]
    if len(items) != len(raw_items):
        raise PublishError("The enrichment output contains an invalid item")
    for item in items:
        hostname = (urlparse(str(item.get("original_url", ""))).hostname or "").lower()
        if any(hostname == host or hostname.endswith("." + host) for host in BLOCKED_CONTENT_HOSTS):
            raise PublishError("The enrichment output includes a blocked content domain")
        if BLOCKED_CONTENT_TERMS.search(json.dumps(item, ensure_ascii=False)):
            raise PublishError("The enrichment output includes blocked content")
    generated_at = latest.get("generated_at")
    if not isinstance(generated_at, str):
        raise PublishError("The enrichment output is missing generated_at")
    try:
        published_at = datetime.fromisoformat(generated_at).astimezone(ZoneInfo("Asia/Shanghai"))
    except ValueError as exc:
        raise PublishError("generated_at is not an ISO timestamp") from exc
    report_date = published_at.date().isoformat()
    reading_minutes = _reading_minutes(items)
    highlights = [str(item.get("title_cn", "")) for item in items[:3] if str(item.get("title_cn", "")).strip()]
    report = {
        "schema_version": 1,
        "category": "ai",
        "report_date": report_date,
        "published_at": published_at.isoformat(),
        "article_count": len(items),
        "estimated_reading_minutes": reading_minutes,
        "highlights": highlights,
        "items": items,
    }
    report_path = site_root / "data" / "daily" / "ai" / f"{report_date}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    index_path = site_root / "data" / "reports.json"
    existing = _read_json(index_path) if index_path.exists() else {"reports": []}
    previous = existing.get("reports")
    if not isinstance(previous, list):
        raise PublishError("reports.json has an invalid reports list")
    summary = {
        "category": "ai", "report_date": report_date, "published_at": published_at.isoformat(),
        "article_count": len(items), "estimated_reading_minutes": reading_minutes, "highlights": highlights,
    }
    reports = [entry for entry in previous if not (isinstance(entry, dict) and entry.get("category") == "ai" and entry.get("report_date") == report_date)]
    reports.append(summary)
    reports.sort(key=lambda entry: str(entry.get("report_date", "")), reverse=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({"schema_version": 1, "reports": reports}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path

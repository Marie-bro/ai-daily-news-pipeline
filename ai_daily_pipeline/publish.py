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
    characters = sum(len(str(item.get("what_happened", item.get("summary_cn", ""))))
                     + len(str(item.get("why_it_matters", item.get("summary_en", "")))) for item in items)
    return max(5, min(10, ceil(characters / 650)))


def _validated_items(raw_items: object, schema_version: int = 1) -> list[dict[str, object]]:
    if not isinstance(raw_items, list) or not raw_items:
        raise PublishError("No validated enrichment items are available to publish")
    items: list[dict[str, object]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise PublishError("The enrichment output contains an invalid item")
        common = ("title_cn", "title_original", "source", "published_at", "original_url")
        tech_fields = ("category", "what_happened", "why_it_matters")
        bilingual_fields = ("title_en", "what_happened_en", "why_it_matters_en")
        required = common + (tech_fields + bilingual_fields if schema_version >= 3 else tech_fields if schema_version >= 2 else ("summary_cn", "summary_en"))
        for field in required:
            if not isinstance(raw_item.get(field), str) or not raw_item[field].strip():
                raise PublishError(f"An enrichment item is missing {field}")
        if raw_item.get("original_language") not in ("en", "zh"):
            raise PublishError("An enrichment item is missing its original language")
        if schema_version >= 2:
            if type(raw_item.get("importance_score")) is not int or not 0 <= raw_item["importance_score"] <= 100:
                raise PublishError("An enrichment item has an invalid importance score")
        else:
            if raw_item.get("translation_language") != ("zh" if raw_item["original_language"] == "en" else "en"):
                raise PublishError("An enrichment item has an invalid translation language")
            for field in ("key_points_original", "translation", "useful_expressions"):
                if not isinstance(raw_item.get(field), list):
                    raise PublishError(f"An enrichment item is missing {field}")
            if not raw_item["key_points_original"] or len(raw_item["key_points_original"]) != len(raw_item["translation"]):
                raise PublishError("An enrichment item has unmatched bilingual key points")
        parsed = urlparse(raw_item["original_url"])
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
            raise PublishError("An enrichment item has an invalid original URL")
        if any(hostname == host or hostname.endswith("." + host) for host in BLOCKED_CONTENT_HOSTS):
            raise PublishError("The enrichment output includes a blocked content domain")
        try:
            source_date = datetime.fromisoformat(raw_item["published_at"])
        except ValueError as exc:
            raise PublishError("An enrichment item has an invalid published_at") from exc
        if source_date.tzinfo is None:
            raise PublishError("An enrichment item published_at needs a timezone")
        if BLOCKED_CONTENT_TERMS.search(json.dumps(raw_item, ensure_ascii=False)):
            raise PublishError("The enrichment output includes blocked content")
        items.append(raw_item)
    return items


def publish_latest_report(pipeline_root: Path, site_root: Path) -> Path:
    latest = _read_json(pipeline_root / "data" / "latest-enrichment.json")
    if latest.get("replay") is True:
        raise PublishError("An isolated historical replay cannot be published as a daily report")
    schema_version = latest.get("schema_version", 1)
    if type(schema_version) is not int or schema_version not in {1, 2, 3}:
        raise PublishError("The enrichment output has an unsupported schema version")
    items = _validated_items(latest.get("items"), schema_version)
    generated_at = latest.get("generated_at")
    if not isinstance(generated_at, str):
        raise PublishError("The enrichment output is missing generated_at")
    try:
        generated = datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise PublishError("generated_at is not an ISO timestamp") from exc
    if generated.tzinfo is None:
        raise PublishError("generated_at needs a timezone")
    published_at = generated.astimezone(ZoneInfo("Asia/Shanghai"))
    report_date = published_at.date().isoformat()
    reading_minutes = _reading_minutes(items)
    highlights = [str(item.get("title_cn", "")) for item in items[:3] if str(item.get("title_cn", "")).strip()]
    report = {
        "schema_version": schema_version,
        "category": "tech" if schema_version >= 2 else "ai",
        "report_date": report_date,
        "published_at": published_at.isoformat(),
        "article_count": len(items),
        "estimated_reading_minutes": reading_minutes,
        "highlights": highlights,
        "items": items,
    }
    index_path = site_root / "data" / "reports.json"
    existing = _read_json(index_path) if index_path.exists() else {"reports": []}
    previous = existing.get("reports")
    if not isinstance(previous, list):
        raise PublishError("reports.json has an invalid reports list")
    summary = {
        "category": "tech" if schema_version >= 2 else "ai", "schema_version": schema_version,
        "report_date": report_date, "published_at": published_at.isoformat(),
        "article_count": len(items), "estimated_reading_minutes": reading_minutes, "highlights": highlights,
    }
    reports = [entry for entry in previous if not (isinstance(entry, dict) and entry.get("report_date") == report_date)]
    reports.append(summary)
    reports.sort(key=lambda entry: str(entry.get("report_date", "")), reverse=True)
    report_path = site_root / "data" / "daily" / "ai" / f"{report_date}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({"schema_version": 1, "reports": reports}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path
